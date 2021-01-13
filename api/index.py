from collections import defaultdict

from requests import get
from jinja2 import Template
from eth_utils import event_abi_to_log_topic, function_abi_to_4byte_selector
from json import loads, dumps
from flask import Flask, jsonify
from flask_cors import CORS

PORT = 3000
ETHERSCAN_API_KEY = '6H8VRTDHJVS6II983YY3DN8NCVBBHDA3MX' # should be env var, but whatever
SOLIDITY_TO_BQ_TYPES = {
  'address': 'STRING',
}
SQL_TEMPLATE_FOR_EVENT = '''
CREATE TEMP FUNCTION
  PARSE_LOG(data STRING, topics ARRAY<STRING>)
  RETURNS STRUCT<{{struct_fields}}>
  LANGUAGE js AS """
    var abi = {{abi}}
    var interface_instance = new ethers.utils.Interface([abi]);
    var parsedLog = interface_instance.parseLog({topics: topics, data: data});
    var parsedValues = parsedLog.values;
    var transformParams = function(params, abiInputs) {
      var result = {};
      if (params && params.length >= abiInputs.length) {
        for (var i = 0; i < abiInputs.length; i++) {
          var paramName = abiInputs[i].name;
          var paramValue = params[i];
          if (abiInputs[i].type === 'address' && typeof paramValue === 'string') {
            // For consistency all addresses are lower-cased.
            paramValue = paramValue.toLowerCase();
          }
          if (ethers.utils.Interface.isIndexed(paramValue)) {
            paramValue = paramValue.hash;
          }
          if (abiInputs[i].type === 'tuple' && 'components' in abiInputs[i]) {
            paramValue = transformParams(paramValue, abiInputs[i].components)
          }
          result[paramName] = paramValue;
        }
      }
      return result;
    };
    var result = transformParams(parsedValues, abi.inputs);
    return result;
"""
OPTIONS
  ( library="gs://blockchain-etl-bigquery/ethers.js" );

WITH parsed_logs AS
(SELECT
    logs.block_timestamp AS block_timestamp
    ,logs.block_number AS block_number
    ,logs.transaction_hash AS transaction_hash
    ,logs.log_index AS log_index
    ,PARSE_LOG(logs.data, logs.topics) AS parsed
FROM `bigquery-public-data.crypto_ethereum.logs` AS logs
WHERE address = '{{contract_address}}'
  AND topics[SAFE_OFFSET(0)] = '{{selector}}'
)
SELECT
     block_timestamp
     ,block_number
     ,transaction_hash
     ,log_index{% for column in columns %}
    ,parsed.{{ column }} AS `{{ column }}`{% endfor %}
FROM parsed_logs
'''

SQL_TEMPLATE_FOR_FUNCTION = '''
CREATE TEMP FUNCTION
    PARSE_TRACE(data STRING)
    RETURNS STRUCT<{{struct_fields}}, error STRING>
    LANGUAGE js AS """
    var abi = {{abi}};
    var interface_instance = new ethers.utils.Interface([abi]);

    var result = {};
    try {
        var parsedTransaction = interface_instance.parseTransaction({data: data});
        var parsedArgs = parsedTransaction.args;

        if (parsedArgs && parsedArgs.length >= abi.inputs.length) {
            for (var i = 0; i < abi.inputs.length; i++) {
                var paramName = abi.inputs[i].name;
                var paramValue = parsedArgs[i];
                if (abi.inputs[i].type === 'address' && typeof paramValue === 'string') {
                    // For consistency all addresses are lowercase.
                    paramValue = paramValue.toLowerCase();
                }
                result[paramName] = paramValue;
            }
        } else {
            result['error'] = 'Parsed transaction args is empty or has too few values.';
        }
    } catch (e) {
        result['error'] = e.message;
    }

    return result;
"""
OPTIONS
  ( library="gs://blockchain-etl-bigquery/ethers.js" );

WITH parsed_traces AS
(SELECT
    traces.block_timestamp AS block_timestamp
    ,traces.block_number AS block_number
    ,traces.transaction_hash AS transaction_hash
    ,traces.trace_address AS trace_address
    ,PARSE_TRACE(traces.input) AS parsed
FROM `bigquery-public-data.crypto_ethereum.traces` AS traces
WHERE to_address = '{{contract_address}}'
  AND STARTS_WITH(traces.input, '{{selector}}')
  )
SELECT
     block_timestamp
     ,block_number
     ,transaction_hash
     ,trace_address
     ,parsed.error AS error
     {% for column in columns %}
    ,parsed.{{ column }} AS `{{ column }}`
    {% endfor %}
FROM parsed_traces
'''

app = Flask(__name__)
CORS(app)

dataset_name = '<INSERT_DATASET_NAME>'
table_prefix = '<TABLE_PREFIX>'
table_description = ''

### UTILS
def read_abi_from_address(address):
  a = address.lower()
  k = ETHERSCAN_API_KEY
  url = f'https://api.etherscan.io/api?module=contract&action=getabi&address={a}&apikey={k}'
  json_response = get(url).json()
  return loads(json_response['result'])

def read_contract_from_address(address):
  a = address.lower()
  k = ETHERSCAN_API_KEY
  url = f'https://api.etherscan.io/api?module=contract&action=getsourcecode&address={a}&apikey={k}'
  json_response = get(url).json()
  contract = [x for x in json_response['result'] if 'ContractName' in x][0]
  return contract

def create_table_name(abi):
  return table_prefix + '_event_' + abi['name']

def abi_to_table_definition(abi, contract_address, parser_type):
  table_name = create_table_name(abi)
  result = {}
  result['parser'] = {
    'type': parser_type,
    'contract_address': contract_address,
    'abi': abi,
    'field_mapping': {}
  }
  result['table'] = {
    'dataset_name': dataset_name,
    'table_name': table_name,
    'table_description': table_description,
    'schema': [
        {
            'name': x.get('name'),
            'description': '',
            'type': 'STRING' # we sometimes get parsing errors, so safest to make all STRING
        } for x in abi['inputs']
    ]
  }
  return result

def contract_to_table_definitions(contract_address):
  abi = read_abi_from_address(contract_address)

  result = {}
  for a in filter_by_type(abi, 'event'):
    abi_item_key = get_abi_item_key(abi, a)
    result[abi_item_key] = abi_to_table_definition(a, contract_address, 'log')
  for a in filter_by_type(abi, 'function'):
    abi_item_key = get_abi_item_key(abi, a)
    result[abi_item_key] = abi_to_table_definition(a, contract_address, 'trace')
  return result


def s2bq_type(type):
  return SOLIDITY_TO_BQ_TYPES.get(type, 'STRING')

def filter_by_type(abi, type):
  for a in abi:
    if a['type'] == type:
      yield a

def get_columns_from_event_abi(event_abi):
  return [a.get('name') for a in event_abi['inputs']]

def create_struct_fields_from_event_abi(event_abi):
  return ', '.join(['`' + a.get('name') + '` ' + s2bq_type(a.get('type')) for a in event_abi['inputs']])

def abi_to_sql(abi, template, contract_address):
  if abi['type'] == 'event':
    selector = '0x' + event_abi_to_log_topic(abi).hex()
  else:
    selector = '0x' + function_abi_to_4byte_selector(abi).hex()

  struct_fields = create_struct_fields_from_event_abi(abi)
  columns = get_columns_from_event_abi(abi)
  return template.render(
    abi=dumps(abi),
    contract_address=contract_address.lower(),
    selector=selector,
    struct_fields=struct_fields,
    columns=columns
  )


def get_abi_item_key(abi, abi_item):
  name_counts = defaultdict(int)
  for a in abi:
    if 'name' in a:
      name_counts[a['name']] += 1

  is_ambiguous = name_counts[abi_item['name']] > 1

  key = abi_item['name']
  if is_ambiguous:
    input_types = [i['type'] for i in abi_item.get('inputs', [])]
    if input_types:
      key = abi_item['name'] + '_' + '_'.join(input_types)
  return key

def contract_to_sqls(contract_address):
  abi = read_abi_from_address(contract_address)

  event_tpl = Template(SQL_TEMPLATE_FOR_EVENT)
  function_tpl = Template(SQL_TEMPLATE_FOR_FUNCTION)

  result = {}
  for a in filter_by_type(abi, 'event'):
    abi_item_key = get_abi_item_key(abi, a)
    result[abi_item_key] = abi_to_sql(a, event_tpl, contract_address)
  for a in filter_by_type(abi, 'function'):
    abi_item_key = get_abi_item_key(abi, a)
    result[abi_item_key] = abi_to_sql(a, function_tpl, contract_address)
  return result

### WEB SERVER

@app.route('/api/')
def index():
    return jsonify({'status': 'alive'})

@app.route('/api/test')
def test():
    return jsonify({'status': 'test'})


@app.route('/api/queries/<contract>')
def queries(contract):
    queries = contract_to_sqls(contract)
    return jsonify(queries)

@app.route('/api/tables/<contract>')
def tables(contract):
    tables = contract_to_table_definitions(contract)
    return jsonify(tables)

@app.route('/api/contract/<contract>')
def contract(contract):
    c = read_contract_from_address(contract)
    return jsonify(c)

if __name__ == "__main__":
    app.run(debug=True, host='127.0.0.1', port=PORT)
