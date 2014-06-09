/** @jsx React.DOM */
'use strict';
var React = require('react');
var FetchedData = require('../fetched').FetchedData;
var globals = require('../globals');
var search = require('../search');

var Listing = search.Listing;
var ResultTable = search.ResultTable;

var ReactForms = require('react-forms');
var Schema = ReactForms.schema.Schema;
var Property = ReactForms.schema.Property;


var SearchBlockView = React.createClass({
    render: function() {
        var context = this.props.data;
        var results = context['@graph'];
        var columns = context['columns'];
        return (
            <div className="panel">
                <ul className="nav result-table">
                    {results.length ?
                        results.map(function (result) {
                            return Listing({context: result, columns: columns, key: result['@id']});
                        })
                    : null}
                </ul>
            </div>
        );
    }
});


var SearchBlockEdit = React.createClass({
    render: function() {
        var styles = {maxHeight: 300, overflow: 'scroll' };
        return (
            <div className="well" style={styles}>
                {this.transferPropsTo(<ResultTable context={this.props.data} />)}
            </div>
        );
    }
});


var FetchedSearch = React.createClass({

    shouldComponentUpdate: function(nextProps) {
        return (nextProps.value != this.props.value);
    },

    render: function() {
        if (this.props.mode === 'edit') {
            var url = '/search' + this.props.value;
            return <FetchedData url={url} Component={SearchBlockEdit} loadingComplete={true}
                                searchBase={this.props.value} onChange={this.props.onChange} />;
        } else {
            var url = '/search' + this.props.value.search;
            return <FetchedData url={url} Component={SearchBlockView} loadingComplete={true} />;
        }
    }
});


globals.blocks.register({
    label: 'search block',
    icon: 'icon-search',
    schema: (
        <Schema>
          <Property name="search" label="Search Criteria" input={<FetchedSearch mode="edit" />} />
        </Schema>
    ),
    view: FetchedSearch
}, 'searchblock');
