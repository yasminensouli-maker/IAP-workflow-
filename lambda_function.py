import json
import base64
import boto3
import time
import os

# IAP Funding Workflow backend
# Handles: save deal, list deals, get deal, upload Cost Explorer file
# Storage: DynamoDB table 'iap-deals', S3 bucket set via env var BUCKET

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
TABLE = os.environ.get('TABLE', 'iap-deals')
BUCKET = os.environ.get('BUCKET', '')

def lambda_handler(event, context):
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Content-Type': 'application/json'
    }

    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    if method == 'OPTIONS':
        return {'statusCode': 200, 'headers': headers, 'body': '{}'}

    try:
        path = event.get('rawPath', '/')
        body = {}
        if event.get('body'):
            raw = event['body']
            if event.get('isBase64Encoded'):
                raw = base64.b64decode(raw).decode('utf-8')
            body = json.loads(raw)

        table = dynamodb.Table(TABLE)

        # ── SAVE DEAL ──
        if path == '/deal' and method == 'POST':
            deal = body.get('deal', {})
            if not deal.get('id'):
                deal['id'] = str(int(time.time() * 1000))
            deal['updatedAt'] = int(time.time())
            table.put_item(Item=json.loads(json.dumps(deal), parse_float=str))
            return ok(headers, {'saved': True, 'id': deal['id']})

        # ── LIST DEALS ──
        if path == '/deals' and method == 'GET':
            resp = table.scan()
            items = [d for d in resp.get('Items', []) if not str(d.get('id', '')).startswith('config#')]
            return ok(headers, {'deals': items})

        # ── CONFIG (budget pool, shared settings) ──
        if path == '/config' and method == 'POST':
            key = body.get('key', '')
            value = body.get('value', '')
            table.put_item(Item={'id': 'config#' + key, 'value': str(value), 'updatedAt': int(time.time())})
            return ok(headers, {'saved': True, 'key': key})

        if path == '/config' and method == 'GET':
            key = event.get('queryStringParameters', {}).get('key', '') if event.get('queryStringParameters') else ''
            resp = table.get_item(Key={'id': 'config#' + key})
            item = resp.get('Item', {})
            return ok(headers, {'key': key, 'value': item.get('value')})

        # ── UPLOAD COST EXPLORER FILE ──
        if path == '/upload' and method == 'POST':
            filename = body.get('filename', 'file')
            filedata = body.get('data', '')  # base64
            deal_id = body.get('dealId', 'unassigned')
            key = f"cost-explorer/{deal_id}/{int(time.time())}-{filename}"
            s3.put_object(
                Bucket=BUCKET,
                Key=key,
                Body=base64.b64decode(filedata),
                ServerSideEncryption='AES256'
            )
            return ok(headers, {'uploaded': True, 'key': key})

        return {'statusCode': 404, 'headers': headers, 'body': json.dumps({'error': 'not found'})}

    except Exception as e:
        return {'statusCode': 500, 'headers': headers, 'body': json.dumps({'error': str(e)})}

def ok(headers, data):
    return {'statusCode': 200, 'headers': headers, 'body': json.dumps(data, default=str)}
