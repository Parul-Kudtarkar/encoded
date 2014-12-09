from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError
from pyramid.events import (
    BeforeRender,
    subscriber,
)
from elasticsearch.connection import Urllib3HttpConnection
from elasticsearch.serializer import SerializationError
from pyramid.view import view_config
from uuid import UUID
from .contentbase import (
    AfterModified,
    BeforeModified,
    Created,
)
from .embedding import embed
from .renderers import json_renderer
from .stats import ElasticsearchConnectionMixin
from .storage import (
    DBSession,
    TransactionRecord,
)
import functools
import json
import logging
import multiprocessing
import transaction
import itertools

log = logging.getLogger(__name__)
ELASTIC_SEARCH = __name__ + ':elasticsearch'
INDEX = 'encoded'


def includeme(config):
    config.add_route('index', '/index')
    config.scan(__name__)

    if 'elasticsearch.server' in config.registry.settings:
        es = Elasticsearch(
            [config.registry.settings['elasticsearch.server']],
            serializer=PyramidJSONSerializer(json_renderer),
            connection_class=TimedUrllib3HttpConnection,
        )
        # es.session.hooks['response'].append(requests_timing_hook('es'))
        config.registry[ELASTIC_SEARCH] = es


class PyramidJSONSerializer(object):
    mimetype = 'application/json'

    def __init__(self, renderer):
        self.renderer = renderer

    def loads(self, s):
        try:
            return json.loads(s)
        except (ValueError, TypeError) as e:
            raise SerializationError(s, e)

    def dumps(self, data):
        # don't serialize strings
        if isinstance(data, (type(''), type(u''))):
            return data

        try:
            return self.renderer.dumps(data)
        except (ValueError, TypeError) as e:
            raise SerializationError(data, e)


class TimedUrllib3HttpConnection(ElasticsearchConnectionMixin, Urllib3HttpConnection):
    pass


@view_config(route_name='index', request_method='POST', permission="index")
def index(request):
    record = request.json.get('record', False)
    dry_run = request.json.get('dry_run', False)
    es = request.registry.get(ELASTIC_SEARCH, None)

    session = DBSession()
    connection = session.connection()
    # http://www.postgresql.org/docs/9.3/static/functions-info.html#FUNCTIONS-TXID-SNAPSHOT
    query = connection.execute("""
        SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY, DEFERRABLE;
        SELECT txid_snapshot_xmin(txid_current_snapshot()), pg_export_snapshot();
    """)
    result, = query.fetchall()
    xmin, snapshot_id = result  # lowest xid that is still in progress

    last_xmin = None
    if 'last_xmin' in request.json:
        last_xmin = request.json['last_xmin']
    elif es is not None:
        try:
            status = es.get(index=INDEX, doc_type='meta', id='indexing')
        except NotFoundError:
            pass
        else:
            last_xmin = status['_source']['xmin']

    result = {
        'xmin': xmin,
        'last_xmin': last_xmin,
    }

    if last_xmin is None:
        result['types'] = types = request.json.get('types', None)
        invalidated = all_uuids(request.root, types)
    else:
        txns = session.query(TransactionRecord).filter(
            TransactionRecord.xid >= last_xmin,
        )

        invalidated = set()
        updated = set()
        max_xid = 0
        txn_count = 0
        for txn in txns.all():
            txn_count += 1
            max_xid = max(max_xid, txn.xid)
            invalidated.update(UUID(uuid) for uuid in txn.data.get('invalidated', ()))
            updated.update(UUID(uuid) for uuid in txn.data.get('updated', ()))

        if txn_count == 0:
            max_xid = None

        new_referencing = set()
        add_dependent_objects(request.root, updated, new_referencing)
        invalidated.update(new_referencing)
        result.update(
            max_xid=max_xid,
            txn_count=txn_count,
            invalidated=[str(uuid) for uuid in invalidated],
        )

    if not dry_run and es is not None:
        result['count'] = count = es_update_object(request, invalidated, snapshot_id)
        if count and record:
            es.index(index=INDEX, doc_type='meta', body=result, id='indexing')

        es.indices.refresh(index=INDEX)

    return result


def all_uuids(root, types=None):
    # First index user and access_key so people can log in
    initial = ['user', 'access_key']
    for collection_name in initial:
        collection = root.by_item_type[collection_name]
        if types is not None and collection_name not in types:
            continue
        for count, uuid in enumerate(collection):
            yield uuid
    for collection_name in sorted(root.by_item_type):
        if collection_name in initial:
            continue
        if types is not None and collection_name not in types:
            continue
        collection = root.by_item_type[collection_name]
        for count, uuid in enumerate(collection):
            yield uuid


def add_dependent_objects(root, new, existing):
    # Getting the dependent objects for the indexed object
    objects = new.difference(existing)
    while objects:
        dependents = set()
        for uuid in objects:
            item = root.get_by_uuid(uuid)

            dependents.update({
                model.source_rid for model in item.model.revs
            })

            item_type = item.item_type
            item_rels = item.model.rels
            for rel in item_rels:
                key = (item_type, rel.rel)
                if key not in root.all_merged_rev:
                    continue
                rev_item = root.get_by_uuid(rel.target_rid)
                if key in rev_item.merged_rev.values():
                    dependents.add(rel.target_rid)

        existing.update(objects)
        objects = dependents.difference(existing)


def make_pool(settings):
    from multiprocessing.pool import IMapIterator

    def wrapper(func):
        def wrap(self, timeout=None):
            # Note: the timeout of 1 googol seconds introduces a rather subtle
            # bug for Python scripts intended to run many times the age of the universe.
            return func(self, timeout=timeout if timeout is not None else 1e100)
        return wrap
    IMapIterator.next = wrapper(IMapIterator.next)

    import atexit
    event = multiprocessing.Event()
    pool = multiprocessing.Pool(
        initializer=pool_initializer,
        initargs=(settings, event),
    )

    @atexit.register
    def abort():
        pool.terminate()
        pool.join()

    return pool, event

_pool_app = None
_pool_event = None


def pool_initializer(settings, event):
    from encoded import main
    global _pool_app
    global _pool_event

    _pool_app = main(settings, indexer=False, create_tables=False)
    _pool_event = event


def pool_set_snapshot_id(snapshot_id):
    _pool_event.wait()
    from pyramid.threadlocal import manager
    import transaction
    txn = transaction.begin()
    txn.doom()
    txn.setExtendedInfo('snapshot_id', snapshot_id)
    app = _pool_app
    root = app.root_factory(app)
    registry = app.registry
    request = app.request_factory.blank('/_indexing_pool')
    extensions = app.request_extensions
    if extensions is not None:
        request._set_extensions(extensions)
    request.invoke_subrequest = app.invoke_subrequest
    request.root = root
    request.registry = registry
    request._stats = {}
    manager.push({'request': request, 'registry': registry})


def pool_clear_snapshot_id(snapshot_id):
    _pool_event.wait()
    from pyramid.threadlocal import manager
    import transaction
    transaction.abort()
    manager.pop()


def pool_embed(uuid):
    from pyramid.threadlocal import get_current_request
    request = get_current_request()
    try:
        return uuid, embed(request, '/%s/@@index-data' % uuid, as_user='INDEXER'), None
    except Exception:
        import traceback
        from cStringIO import StringIO
        buf = StringIO()
        traceback.print_exc(file=buf)
        print buf.getvalue()
        return uuid, None, buf.getvalue()


def es_update_object(request, objects, snapshot_id):
    if not objects:
        return 0

    es = request.registry[ELASTIC_SEARCH]
    pool, event = request.registry['indexing_pool']
    if pool:
        imap = pool.imap_unordered
    else:
        imap = itertools.imap
    try:
        if pool:
            event.clear()
            result = pool.map_async(pool_set_snapshot_id, (snapshot_id for x in range(pool._processes)), 1)
            event.set()
            result.get()

        results = imap(pool_embed, (str(uuid) for uuid in objects))

        for i, item in enumerate(results):
            uuid, result, error = item
            if error is not None:
                log.warning('Error indexing %s\n%s', uuid, error)
            else:
                doctype = result['object']['@type'][0]
                try:
                    es.index(index=INDEX, doc_type=doctype, body=result, id=str(uuid))
                except Exception:
                    log.warning('Error indexing %s', uuid, exc_info=True)
                else:
                    if (i + 1) % 50 == 0:
                        log.info('Indexing %s %d', result['object']['@id'], i + 1)

            if (i + 1) % 50 == 0:
                es.indices.flush(index=INDEX)

        return i + 1
    finally:
        if pool:
            event.clear()
            result = pool.map(pool_clear_snapshot_id, (snapshot_id for x in range(pool._processes)), 1)
            event.set()
            result.get()

def run_in_doomed_transaction(fn, committed, *args, **kw):
    if not committed:
        return
    txn = transaction.begin()
    txn.doom()  # enables SET TRANSACTION READ ONLY;
    try:
        fn(*args, **kw)
    finally:
        txn.abort()


# After commit hook needs own transaction
es_update_object_in_txn = functools.partial(
    run_in_doomed_transaction, es_update_object,
)


@subscriber(Created)
@subscriber(BeforeModified)
@subscriber(AfterModified)
def record_created(event):
    request = event.request
    # Create property if that doesn't exist
    try:
        referencing = request._encoded_referencing
    except AttributeError:
        referencing = request._encoded_referencing = set()
    try:
        updated = request._encoded_updated
    except AttributeError:
        updated = request._encoded_updated = set()

    uuid = event.object.uuid
    updated.add(uuid)

    # Record dependencies here to catch any to be removed links
    # XXX replace with uuid_closure in elasticsearch document
    add_dependent_objects(request.root, {uuid}, referencing)


@subscriber(BeforeRender)
def es_update_data(event):
    request = event['request']
    updated = getattr(request, '_encoded_updated', None)

    if not updated:
        return

    invalidated = getattr(request, '_encoded_referencing', set())

    txn = transaction.get()
    txn._extension['updated'] = [str(uuid) for uuid in updated]
    txn._extension['invalidated'] = [str(uuid) for uuid in invalidated]

    # XXX How can we ensure consistency here but update written records
    # immediately? The listener might already be indexing on another
    # connection. SERIALIZABLE isolation insufficient because ES writes not
    # serialized. Could either:
    # - Queue up another reindex on the listener
    # - Use conditional puts to ES based on serial before commit.
    # txn = transaction.get()
    # txn.addAfterCommitHook(es_update_object_in_txn, (request, updated))
