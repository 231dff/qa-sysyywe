# mcp_server.py
from mcp.server.fastmcp import FastMCP

# 创建 MCP Server
mcp = FastMCP("WeatherServer")

@mcp.tool()
def get_weather(city: str) -> str:
    """获取指定城市的实时天气情况"""
    if "北京" in city:
        return "北京今天天气晴朗，气温 25 度，微风。"
    elif "上海" in city:
        return "上海今天有中雨，气温 20 度，请带伞。"
    return f"未找到 {city} 的天气信息。"

@mcp.tool()
def get_air_quality(city: str) -> str:
    """获取指定城市的空气质量指数 (AQI)"""
    return f"{city} 今天的 AQI 为 45，空气质量优。"

if __name__ == "__main__":
    # 以 stdio (标准输入输出) 模式运行
    mcp.run(transport="stdio")