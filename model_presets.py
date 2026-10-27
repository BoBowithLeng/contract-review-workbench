"""Public provider presets shared by both UIs; never contains credentials."""

PRESETS = {
    "DeepSeek": ("https://api.deepseek.com", "deepseek-flash"),
    "硅基流动 SiliconFlow": ("https://api.siliconflow.cn/v1", ""),
    "OpenRouter": ("https://openrouter.ai/api/v1", ""),
    "自定义兼容接口": ("", ""),
}


def providers():
    return [{"id": name, "label": name, "base_url": values[0], "model": values[1]}
            for name, values in PRESETS.items()]
