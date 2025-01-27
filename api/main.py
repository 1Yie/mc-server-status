import time
from flask import Flask, jsonify, request
from mcstatus import JavaServer
from flask_cors import CORS
from mcrcon import MCRcon, MCRconException
import os
import re
import logging
from dotenv import load_dotenv
from functools import lru_cache
from typing import Dict, List, Tuple
import json
from pathlib import Path

# 初始化环境变量
load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 记录服务器启动时间
SERVER_START_TIME = time.time()

# 创建Flask应用
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False  # 禁用ASCII编码转义
CORS(app)


# 服务器配置校验
def validate_config() -> Tuple[str, int, str, str]:
    """验证并返回服务器配置"""
    try:
        rcon_host = os.environ["RCON_HOST"]
        rcon_port = int(os.environ.get("RCON_PORT", "25575"))
        rcon_password = os.environ["RCON_PASSWORD"]
        server_address = os.environ["SERVER_ADDRESS"]
        return rcon_host, rcon_port, rcon_password, server_address
    except (KeyError, ValueError) as e:
        logger.critical(f"配置错误: {str(e)}")
        raise


RCON_HOST, RCON_PORT, RCON_PASSWORD, SERVER_ADDRESS = validate_config()

# 维度映射（带缓存）
DIMENSION_MAP_PATH = Path("dimension_map.json")


def load_dimension_map() -> dict:
    """从配置文件加载维度映射"""
    if DIMENSION_MAP_PATH.exists():
        try:
            with open(DIMENSION_MAP_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"配置文件加载失败: {str(e)}")
            return {}
    return {}


@lru_cache(maxsize=32)
def get_dimension_display_name(raw_dim: str) -> str:
    """获取维度的友好显示名称"""
    dimension_map = load_dimension_map()
    return dimension_map.get(raw_dim, raw_dim.split(":")[-1])


# RCON客户端封装

class RCONClient:
    """带重试机制的RCON客户端"""

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self.retries = 3
        self.conn = None  # 连接实例

    def connect(self):
        if self.conn is None:
            for i in range(self.retries):
                try:
                    self.conn = MCRcon(self.host, self.password, self.port)
                    self.conn.connect()
                    logger.info("成功连接到RCON")
                    return
                except MCRconException as e:
                    if i == self.retries - 1:
                        logger.critical(f"RCON连接失败: {str(e)}")
                        raise
                    logger.warning(f"RCON连接失败，第{i + 1}次重试... 错误: {str(e)}")
                    time.sleep(2 ** i)  # 指数退避

    def disconnect(self):
        """断开RCON连接"""
        if self.conn:
            self.conn.disconnect()
            self.conn = None
            logger.info("RCON连接已断开")

    def execute(self, command: str) -> str:
        """执行RCON命令"""
        self.connect()  # 确保已连接
        try:
            return self.conn.command(command)
        except MCRconException as e:
            logger.error(f"RCON命令执行失败: {command} - {str(e)}")
            raise

    def __enter__(self):
        self.connect()  # 在进入时连接
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()  # 在退出时断开连接


# 全局RCON客户端
rcon_client = RCONClient(RCON_HOST, RCON_PORT, RCON_PASSWORD)
rcon_client.connect()  # 在应用启动时建立连接


# 玩家数据解析
class PlayerDataParser:
    """玩家数据解析器"""
    POS_PATTERN = re.compile(r"Pos:\s*\[([\d., dE+-]+)]")
    DIMENSION_PATTERN = re.compile(r'Dimension:\s*"([^"]+)"')
    PLAYERNAME_PATTERN = re.compile(r"([a-zA-Z0-9_]{3,16})(?:,|$)")
    HEALTH_PATTERN = re.compile(r"Health:\s*([0-9.]+)[fs]")
    FOOD_PATTERN = re.compile(r"(?:foodLevel|food):\s*(\d+)(?:s|b|)")
    LEVEL_PATTERN = re.compile(r"(?:XpLevel|level):\s*(\d+)(?:s|b|)")

    @classmethod
    def parse_players(cls, list_response: str) -> List[str]:
        """从list命令响应中解析玩家列表"""
        matches = cls.PLAYERNAME_PATTERN.findall(list_response)
        return list(set(matches)) if matches else []

    @classmethod
    def parse_entity_data(cls, response: str) -> Dict:
        """解析实体数据（严格模式，无默认值）"""
        result = {
            "pos": None,
            "dimension": None,
            "health": None,
            "food": None,
            "level": None
        }

        try:
            # 坐标解析
            pos_match = cls.POS_PATTERN.search(response)
            if pos_match:
                pos_str = pos_match.group(1).replace('d', '').strip()
                try:
                    x, y, z = map(float, pos_str.split(", "))
                    result["pos"] = (x, y, z)
                except ValueError:
                    logger.warning(f"坐标格式错误: {pos_str}")

            # 维度解析
            dim_match = cls.DIMENSION_PATTERN.search(response)
            if dim_match:
                raw_dim = dim_match.group(1)
                result["dimension"] = {
                    "raw": raw_dim,
                    "display": get_dimension_display_name(raw_dim)
                }

            # 生命值解析
            health_match = cls.HEALTH_PATTERN.search(response)
            if health_match:
                health_val = health_match.group(1)
                try:
                    result["health"] = float(health_val) if '.' in health_val else int(health_val)
                except (ValueError, TypeError):
                    logger.warning(f"生命值转换失败: {health_val}")

            # 饱食度解析
            food_match = cls.FOOD_PATTERN.search(response)
            if food_match:
                try:
                    result["food"] = int(food_match.group(1))
                except (ValueError, TypeError):
                    logger.warning(f"饱食度转换失败: {food_match.group(1)}")

            # 等级解析
            level_match = cls.LEVEL_PATTERN.search(response)
            if level_match:
                try:
                    result["level"] = int(level_match.group(1))
                except (ValueError, TypeError):
                    logger.warning(f"等级转换失败: {level_match.group(1)}")

        except Exception as e:
            logger.error(f"全局解析异常: {str(e)}")

        # 清理空值字段
        return {k: v for k, v in result.items() if v is not None}


# API 端点实现
@app.route('/api/player/avatar', methods=['GET'])
def get_avatar():
    """获取玩家头像"""
    if not (uuid := request.args.get('uuid')):
        return jsonify({"error": "缺少UUID参数"}), 400
    return jsonify({
        "uuid": uuid,
        "avatar_url": f"https://crafatar.com/avatars/{uuid}"
    })


@app.route('/api/server/status', methods=['GET'])
def get_server_status():
    """获取服务器状态"""
    try:
        server = JavaServer(SERVER_ADDRESS, timeout=5)
        status = server.status()
        players = [{
            "name": p.name,
            "uuid": p.id,
            "avatar_url": f"https://crafatar.com/avatars/{p.id}"
        } for p in status.players.sample] if status.players.sample else []

        # 获取主世界时间和游戏运行时间
        world_time = None
        game_time = None
        try:
            time_response = rcon_client.execute("time query daytime")
            if "The time is" in time_response:
                world_time = int(time_response.split(" ")[-1])
            game_time_response = rcon_client.execute("time query gametime")
            if "The time is" in game_time_response:
                game_time = int(game_time_response.split(" ")[-1])
        except Exception as e:
            logger.warning(f"获取时间失败: {str(e)}")

        # 计算服务器运行时间
        uptime_seconds = int(time.time() - SERVER_START_TIME)
        uptime_formatted = format_uptime(uptime_seconds)

        return jsonify({
            "online": status.players.online,
            "latency": status.latency,
            "version": status.version.name,
            "players": players,
            "world_time": world_time,
            "world_time_formatted": format_minecraft_time(world_time) if world_time is not None else None,
            "game_time": game_time,
            "game_time_formatted": format_minecraft_time(game_time) if game_time is not None else None,
            "uptime_seconds": uptime_seconds,
            "uptime_formatted": uptime_formatted
        })
    except Exception as e:
        logger.error(f"服务器状态查询失败: {str(e)}")
        return jsonify({"error": "无法获取服务器状态"}), 500


def format_minecraft_time(time_ticks: int) -> str:
    """将 Minecraft 游戏刻转换为可读时间格式 (HH:MM)"""
    if time_ticks is None:
        return "未知"
    time_ticks %= 24000  # 确保时间在一天内
    hours = (time_ticks // 1000 + 6) % 24
    minutes = int((time_ticks % 1000) / 1000 * 60)
    return f"{hours:02d}:{minutes:02d}"


def format_uptime(seconds: int) -> str:
    """将秒数格式化为 HH:MM:SS"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@app.route('/api/server/player_info', methods=['GET'])
def get_player_infos():
    """获取玩家信息"""
    try:
        list_resp = rcon_client.execute("list")
        logger.debug(f"list命令响应: {list_resp}")
        players = PlayerDataParser.parse_players(list_resp)

        results = []
        for player in players:
            try:
                entity_resp = rcon_client.execute(f"data get entity {player}")
                data = PlayerDataParser.parse_entity_data(entity_resp)

                # 校验必要字段
                if not data.get("pos") or not data.get("dimension"):
                    logger.warning(f"玩家 {player} 数据不完整，跳过")
                    continue

                results.append({
                    "name": player,
                    "world": data["dimension"]["display"],
                    "raw_dimension": data["dimension"]["raw"],
                    "location": {
                        "x": round(data["pos"][0], 2),
                        "y": round(data["pos"][1], 2),
                        "z": round(data["pos"][2], 2)
                    },
                    "status": {
                        k: v for k, v in data.items()
                        if k in ["health", "food", "level"] and v is not None
                    }
                })
            except Exception as e:
                logger.warning(f"玩家 {player} 数据获取失败: {str(e)}")
        return jsonify(results)
    except Exception as e:
        logger.error(f"RCON操作失败: {str(e)}")
        return jsonify({"error": "服务器连接失败"}), 503


# 运行应用
if __name__ == '__main__':
    try:
        app.run(host='0.0.0.0', port=5000, debug=False)
    except KeyboardInterrupt:
        logger.info("服务器关闭...")
        rcon_client.disconnect()