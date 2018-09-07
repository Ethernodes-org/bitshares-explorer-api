from elasticsearch_dsl import connections, Search, Q, A
import config

class BitsharesElasticSearchClient():
    def __init__(self, default_cluster_config, additional_clusters_config):
        self._create_connection('operations', default_cluster_config, additional_clusters_config)
        self._create_connection('objects', default_cluster_config, additional_clusters_config)

    def _create_connection(self, name, default_cluster_config, additional_clusters_config):
        config = default_cluster_config
        if additional_clusters_config and name in additional_clusters_config and additional_clusters_config[name]:
            config = additional_clusters_config[name]
    
        connection_config = {
            'hosts': config['hosts'],
            'timeout': 60
        }
        if 'user' in config and config['user']  \
            and 'password' in config and config['password']:
            connection_config['http_auth'] = (config['user'], config['password'])
        
        connections.create_connection(name, **connection_config)
        

    def get_markets(self, from_date, to_date, base=None, quote=None):
        query = {
            "size": 0,  
            "query": {
                "bool": {
                    "filter": [
                        { "term": { "operation_type": 4 } },
                        { 
                            "range": { 
                                "block_data.block_time": { 
                                    "gte": from_date, 
                                    "lte": to_date
                                } 
                            } 
                        }
                    ]
                }
            },
            "aggs": {
                "pairs": {
                    "composite" : {
                        "size": 10000, # TODO use a generator function instead of a big size, see https://github.com/elastic/elasticsearch-dsl-py/blob/master/examples/composite_agg.py#L21
                        "sources" : [
                            { "pays_asset": { "terms" : { "field": "additional_data.fill_data.pays_asset_id.keyword" } } },
                            { "recieves_asset": { "terms" : { "field": "additional_data.fill_data.receives_asset_id.keyword" } } }
                        ]
                    },
                    "aggs": {
                        "volume": { "sum" : { "field" : "additional_data.fill_data.receives_amount" } }
                    }
                }
            }
        }

        if base:
            query['query']['bool']['filter'].append({ "term": { "additional_data.fill_data.pays_asset_id": base } })
        if quote:
            query['query']['bool']['filter'].append({ "term": { "additional_data.fill_data.receives_asset_id": quote } })

        client = connections.get_connection('operations')
        response = client.search(index="bitshares-*", body=query)

        markets = {}
        for bucket in response['aggregations']['pairs']['buckets']:
            pays_asset = bucket['key']['pays_asset']
            recieves_asset = bucket['key']['recieves_asset']
            volume = bucket['volume']['value']
            nb_operations = bucket['doc_count']

            if pays_asset not in markets:
                markets[pays_asset] = {}
            markets[pays_asset][recieves_asset] = { 'volume': volume, 'nb_operations': nb_operations }

        return markets

    # This is only to keep a trace of the code somewhere as it does not work due to a bug in elasticsearch-dsl.
    def _get_markets_with_dsl(self, from_date, to_date):
        # Could not use DSL due to a bug on multi sources composite aggregation:
        # https://github.com/elastic/elasticsearch-dsl-py/issues/963

        s = Search(using='operations', index="bitshares-*")
        s = s.extra(size=0)
        s = s.query('bool', filter = [
            Q('term', operation_type=4),
            Q("range", block_data__block_time={'gte': from_date, 'lte': to_date})
        ])

        sources = [ 
            { 'pays_asset': A('terms', field='additional_data.fill_data.pays_asset_id.keyword') },
            { 'recieves_asset': A('terms', field='additional_data.fill_data.receives_asset_id.keyword') }
        ]

        # Bug here as 'sources' does not support a list.
        a = A('composite', sources=sources, size=10000).metric('volume', 'sum', field='additional_data.fill_data.receives_amount')
        s.aggs.bucket('pairs', a)
        response = s.execute()

        # TODO...

    def get_asset_ids(self):
        # FIXME: should use scan() or iterate over results.
        s = Search(using='objects', index="objects-asset") \
            .extra(size=10000)                             \
            .query('match_all')                            \
            .source(['object_id'])

        response = s.execute()
        asset_ids = [ hit.object_id for hit in response]
        return asset_ids

    def get_asset_names(self, start):
        s = Search(using='objects', index="objects-asset") \
            .query('prefix', symbol__keyword=start)              \
            .source(['symbol'])

        response = s.execute()

        asset_names = [ hit.symbol for hit in response]
        return asset_names

    def get_daily_volume(self, from_date, to_date):
        s = Search(using='operations', index="bitshares-*")
        s = s.extra(size=0)
        s = s.query('bool', filter = [
            Q('term', operation_type=4),
            Q('range', block_data__block_time={'gte': from_date, 'lte': to_date}),
            Q('term', additional_data__fill_data__receives_asset_id=config.CORE_ASSET_ID)
        ])

        a = A('date_histogram', field='block_data.block_time', interval='1d', format='yyyy-MM-dd') \
                .metric('volume', 'sum', field='additional_data.fill_data.receives_amount')
        s.aggs.bucket('volume_over_time', a)

        response = s.execute()

        daily_volumes = []
        for daily_volume in response.aggregations.volume_over_time.buckets:
            daily_volumes.append({ 'date': daily_volume.key_as_string, 'volume': daily_volume.volume.value })
        
        return daily_volumes

    def get_accounts_with_referrer(self, account_id, size=20, from_=0):
        s = Search(using='objects', index="objects-account", extra={'size': size, 'from': from_})    \
                .filter('term', referrer__keyword=account_id)                                        \
                .source([
                    "object_id", "name", "referrer", 
                    "referrer_rewards_percentage", "lifetime_referrer", 
                    "lifetime_referrer_fee_percentage"])                            \
                .sort("name.keyword")

        response = s.execute()

        referrers = [hit.to_dict() for hit in response.hits]
        return (response.hits.total, referrers)

    def get_balances(self, account_id=None, asset_id=None):
        s = Search(using='objects', index="objects-balance")
        s = s.extra(size=10000) # FIXME: should use scan() or iterate over results.
        if account_id:
            s = s.filter('term', owner=account_id)
        if asset_id:
            s = s.filter('term', asset_type=asset_id)
        s = s.source([ 'owner', 'balance', 'asset_type'])
        s = s.sort({ 'balance': { 'order': 'desc' } })

        response = s.execute()

        balances = [hit.to_dict() for hit in response.hits]
        return balances

    def get_accounts(self, account_ids):
        s = Search(using='objects', index="objects-account")
        s = s.extra(size=10000) # FIXME: should use scan() or iterate over results.
        s = s.filter('terms', object_id=account_ids)
        s = s.source([ 'object_id', 'name', 'voting_account'])

        response = s.execute()

        accounts = [hit.to_dict() for hit in response.hits]
        return accounts



client = BitsharesElasticSearchClient(config.ELASTICSEARCH, config.ELASTICSEARCH_ADDITIONAL)
es = connections.get_connection(alias='operations')

if __name__ == "__main__":
    import pprint
    balances = client.get_balances(asset_id='1.3.0')
    account_ids = [ balance['owner'] for balance in balances ]
    accounts = client.get_accounts(account_ids)
    result = {}
    for balance in balances:
        result[balance['owner']] = balance
    for account in accounts:
        result[account['object_id']]['owner'] = account
    pprint.pprint(result.values())
