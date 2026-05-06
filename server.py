"""牛牛游戏 WebSocket 服务器"""
import json
import uuid
import socket
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from game import GameRoom, BET_MODES

app = FastAPI()

rooms: dict[str, GameRoom] = {}           # room_id -> GameRoom
connections: dict[str, dict] = {}         # ws_id -> {"ws": WebSocket, "room": str, "name": str}
player_rooms: dict[str, set] = {}         # room_id -> {ws_id, ...}


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


async def broadcast_room(room_id: str):
    """向房间内所有玩家广播游戏状态"""
    if room_id not in rooms or room_id not in player_rooms:
        return
    room = rooms[room_id]
    for ws_id in list(player_rooms[room_id]):
        if ws_id not in connections:
            continue
        conn = connections[ws_id]
        try:
            state = room.get_state(viewer=conn["name"])
            await conn["ws"].send_json({"type": "game_state", "data": state})
        except Exception:
            pass
    # 同步推送给管理后台
    await broadcast_admin(room_id)


admin_connections: dict[str, dict] = {}  # admin_ws_id -> {"ws": WebSocket, "room": str}


async def broadcast_admin(room_id: str):
    """向管理后台推送完整状态"""
    if room_id not in rooms:
        return
    room = rooms[room_id]
    state = room.admin_get_full_state()
    for aid, aconn in list(admin_connections.items()):
        if aconn["room"] == room_id:
            try:
                await aconn["ws"].send_json({"type": "admin_state", "data": state})
            except Exception:
                pass


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_id = str(uuid.uuid4())

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            action = msg.get("action")

            if action == "create_room":
                name = msg["name"].strip()
                if not name:
                    await ws.send_json({"type": "error", "message": "请输入昵称"})
                    continue
                room_id = _generate_room_id()
                settings = msg.get("settings", {})
                room = GameRoom(room_id, name, settings)
                room.add_player(name)
                rooms[room_id] = room
                player_rooms[room_id] = {ws_id}
                connections[ws_id] = {"ws": ws, "room": room_id, "name": name}
                await ws.send_json({"type": "room_created", "room_id": room_id, "name": name, "admin_token": room.admin_token})
                await broadcast_room(room_id)

            elif action == "join_room":
                name = msg["name"].strip()
                room_id = msg["room_id"].strip().upper()
                if not name:
                    await ws.send_json({"type": "error", "message": "请输入昵称"})
                    continue
                if room_id not in rooms:
                    await ws.send_json({"type": "error", "message": "房间不存在"})
                    continue
                room = rooms[room_id]
                if not room.add_player(name):
                    await ws.send_json({"type": "error", "message": "昵称重复或房间已满(最多20人)"})
                    continue
                if room_id not in player_rooms:
                    player_rooms[room_id] = set()
                player_rooms[room_id].add(ws_id)
                connections[ws_id] = {"ws": ws, "room": room_id, "name": name}
                await ws.send_json({"type": "room_joined", "room_id": room_id, "name": name})
                await broadcast_room(room_id)

            elif action == "start_game":
                conn = connections.get(ws_id)
                if not conn:
                    continue
                room = rooms.get(conn["room"])
                if not room or conn["name"] != room.host_name:
                    await ws.send_json({"type": "error", "message": "只有房主能开始游戏"})
                    continue
                if not room.can_start():
                    await ws.send_json({"type": "error", "message": "至少需要2名玩家才能开始"})
                    continue
                room.start_round()
                await broadcast_room(conn["room"])

            elif action == "confirm_cards":
                conn = connections.get(ws_id)
                if not conn:
                    continue
                room = rooms.get(conn["room"])
                if not room:
                    continue
                all_confirmed = room.confirm_cards(conn["name"])
                await broadcast_room(conn["room"])

            elif action == "place_bet":
                conn = connections.get(ws_id)
                if not conn:
                    continue
                room = rooms.get(conn["room"])
                if not room:
                    continue
                bet_action = msg.get("bet_action", "call")
                amount = msg.get("amount", 0)
                result = room.place_bet(conn["name"], bet_action, amount)
                if result.get("ok"):
                    # 检查下注是否完成
                    if room.check_betting_done():
                        solo_win = room.finish_betting()
                    await broadcast_room(conn["room"])
                else:
                    await ws.send_json({"type": "error", "message": "下注失败"})

            elif action == "chat":
                conn = connections.get(ws_id)
                if not conn:
                    continue
                room = rooms.get(conn["room"])
                if not room:
                    continue
                message = msg.get("message", "").strip()
                if not message:
                    continue
                room.add_chat(conn["name"], message)
                await broadcast_room(conn["room"])

            elif action == "set_luck":
                conn = connections.get(ws_id)
                if not conn:
                    continue
                room = rooms.get(conn["room"])
                if not room:
                    continue
                target = msg.get("target", "")
                luck = msg.get("luck", 0)
                room.set_player_luck(conn["name"], target, luck)
                await broadcast_room(conn["room"])

            elif action == "next_round":
                conn = connections.get(ws_id)
                if not conn:
                    continue
                room = rooms.get(conn["room"])
                if not room or conn["name"] != room.host_name:
                    await ws.send_json({"type": "error", "message": "只有房主能开始下一轮"})
                    continue
                room.start_round()
                await broadcast_room(conn["room"])

            elif action == "get_state":
                conn = connections.get(ws_id)
                if not conn:
                    continue
                room = rooms.get(conn["room"])
                if room:
                    state = room.get_state(viewer=conn["name"])
                    await ws.send_json({"type": "game_state", "data": state})

    except WebSocketDisconnect:
        pass
    finally:
        if ws_id in connections:
            conn = connections[ws_id]
            room_id = conn["room"]
            name = conn["name"]
            if room_id in rooms:
                room = rooms[room_id]
                room.remove_player(name)
                if room_id in player_rooms:
                    player_rooms[room_id].discard(ws_id)
                if not room.players:
                    del rooms[room_id]
                    player_rooms.pop(room_id, None)
                else:
                    await broadcast_room(room_id)
            del connections[ws_id]


def _generate_room_id() -> str:
    import random, string
    while True:
        rid = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if rid not in rooms:
            return rid


@app.get("/admin")
async def admin_page():
    return FileResponse(Path(__file__).parent / "static" / "admin.html")


@app.websocket("/ws_admin")
async def admin_websocket(ws: WebSocket):
    await ws.accept()
    admin_ws_id = str(uuid.uuid4())
    room_id = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            action = msg.get("action")

            if action == "auth":
                rid = msg.get("room_id", "").strip().upper()
                token = msg.get("token", "").strip()
                if rid not in rooms:
                    await ws.send_json({"type": "error", "message": "房间不存在"})
                    continue
                room = rooms[rid]
                if not room.admin_verify(token):
                    await ws.send_json({"type": "error", "message": "令牌无效"})
                    continue
                room_id = rid
                admin_connections[admin_ws_id] = {"ws": ws, "room": room_id}
                await ws.send_json({"type": "auth_ok", "room_id": room_id})
                state = room.admin_get_full_state()
                await ws.send_json({"type": "admin_state", "data": state})

            elif action == "admin_set_luck":
                if not room_id or room_id not in rooms:
                    continue
                room = rooms[room_id]
                target = msg.get("target", "")
                luck = msg.get("luck", 0)
                room.set_player_luck(room.host_name, target, luck)
                await broadcast_room(room_id)

            elif action == "admin_set_card":
                if not room_id or room_id not in rooms:
                    continue
                room = rooms[room_id]
                target = msg.get("target", "")
                idx = msg.get("card_index", 0)
                suit = msg.get("suit", "spades")
                rank = msg.get("rank", "A")
                room.admin_set_card(target, idx, suit, rank)
                await broadcast_room(room_id)

            elif action == "admin_set_chips":
                if not room_id or room_id not in rooms:
                    continue
                room = rooms[room_id]
                target = msg.get("target", "")
                chips = msg.get("chips", 0)
                room.admin_set_chips(target, chips)
                await broadcast_room(room_id)

            elif action == "admin_kick":
                if not room_id or room_id not in rooms:
                    continue
                room = rooms[room_id]
                target = msg.get("target", "")
                room.admin_kick(target)
                await broadcast_room(room_id)

            elif action == "admin_next_round":
                if not room_id or room_id not in rooms:
                    continue
                room = rooms[room_id]
                if room.can_start():
                    room.start_round()
                    await broadcast_room(room_id)

            elif action == "admin_force_finish":
                if not room_id or room_id not in rooms:
                    continue
                room = rooms[room_id]
                if room.phase == "playing":
                    for p in room.players:
                        if not p["confirmed"] and not p["folded"]:
                            p["confirmed"] = True
                            from game import evaluate_hand
                            p["result"] = evaluate_hand(p["hand"])
                    room._settle_round()
                    await broadcast_room(room_id)

    except WebSocketDisconnect:
        pass
    finally:
        admin_connections.pop(admin_ws_id, None)


# 挂载静态文件（在路由之后）
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    host = _get_local_ip()
    port = 8000
    print(f"\n{'='*40}")
    print(f"  牛牛游戏服务器已启动!")
    print(f"  本机访问: http://localhost:{port}")
    print(f"  局域网访问: http://{host}:{port}")
    print(f"  把上面的地址发给朋友!")
    print(f"{'='*40}\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
