# -*- coding: utf-8 -*-
"""Listen for incoming Douyin DMs via WebSocket and print full sender info."""
import sys, io, json, hashlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)               # for builder/static/utils
sys.path.insert(0, os.path.join(HERE, 'dy_apis'))  # for douyin_api/static

import websocket
from websocket import WebSocketApp
from douyin_api import DouyinAPI
from builder.auth import DouyinAuth
from builder.header import HeaderBuilder
from builder.params import Params
from static import Live_pb2, Response_pb2
from protobuf_to_dict import protobuf_to_dict

# load auth
cookie_str = [l.strip() for l in open('.env', encoding='utf-8') if l.startswith('DY_COOKIES=')][0][len('DY_COOKIES='):]
keys_str = json.load(open('auth_keys.json', encoding='utf-8'))['keys_str']
auth = DouyinAuth()
auth.perepare_auth(cookie_str, '', keys_str)

appKey = "e1bd35ec9db7b8d846de66ed140b1ad9"
fpId = '9'
deviceId = DouyinAPI.get_device_id(auth=auth)
accessKey = f'{fpId + appKey + deviceId}f8a69f1719916z'
accessKey = hashlib.md5(accessKey.encode('UTF-8')).hexdigest()
params = Params()
params.add_param("aid", "6383")
params.add_param("device_platform", "douyin_pc")
params.add_param("fpid", fpId)
params.add_param("device_id", deviceId)
params.add_param("token", auth.cookie["sessionid"])
params.add_param("access_key", accessKey)
url = f"wss://frontier-im.douyin.com/ws/v2?{params.toString()}"

print("connecting...", flush=True)

def on_open(ws):
    print("OPEN", flush=True)

def on_message(ws, message):
    frame = Live_pb2.PushFrame()
    frame.ParseFromString(message)
    if frame.payloadType == 'pb':
        response = Response_pb2.Response()
        response.ParseFromString(frame.payload)
        # check if it's a new_message_notify
        try:
            notify = response.body.new_message_notify
            msg = notify.message
            sender = msg.sender
            content_raw = msg.content
            msg_type = msg.message_type
            conversation_id = msg.conversation_id
            index = msg.index_in_conversation
            print("=" * 60, flush=True)
            print("MSG_TYPE:", msg_type, "| CONVERSATION_ID:", conversation_id, "| INDEX:", index, flush=True)
            print("SENDER_FULL:", json.dumps(protobuf_to_dict(sender), ensure_ascii=False), flush=True)
            try:
                content = json.loads(content_raw)
                print("CONTENT:", json.dumps(content, ensure_ascii=False), flush=True)
            except Exception as e:
                print("CONTENT_RAW:", repr(content_raw), flush=True)
        except Exception as e:
            print("OTHER_PB msg (not notify):", e, flush=True)
            try:
                print(json.dumps(protobuf_to_dict(response), ensure_ascii=False)[:500], flush=True)
            except:
                pass
    elif frame.payloadType == 'text/json':
        print("TEXT/JSON:", frame.payload, flush=True)
    else:
        print("OTHER payloadType:", frame.payloadType, flush=True)

def on_error(ws, error):
    print("ERROR:", error, flush=True)

def on_close(ws, code, msg):
    print("CLOSED", code, msg, flush=True)

ws = WebSocketApp(
    url=url,
    header={
        'Pragma': 'no-cache',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
        'User-Agent': HeaderBuilder.ua,
        'Cache-Control': 'no-cache',
        'Sec-WebSocket-Protocol': 'binary, base64, pbbp2',
        'Sec-WebSocket-Extensions': 'permessage-deflate; client_max_window_bits',
    },
    cookie=auth.cookie_str,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close,
    on_open=on_open,
)
ws.run_forever(origin='https://www.douyin.com')
