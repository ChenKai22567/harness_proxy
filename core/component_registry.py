from typing import Dict, Tuple

from core.process_model import ComponentDefinition


_COMMON_COMPONENTS = (
    ComponentDefinition(
        "core",
        "核心主进程",
        "负责窗口、生命周期与其余服务的调度",
        "required",
    ),
    ComponentDefinition(
        "renderer",
        "界面渲染器",
        "负责页面、编辑器和交互界面的绘制",
        "required",
    ),
    ComponentDefinition(
        "network",
        "浏览器网络服务",
        "负责 Chromium 页面请求、下载与代理连接",
        "required",
    ),
    ComponentDefinition(
        "gpu",
        "GPU 加速",
        "负责图形合成；关闭后可兼容部分显卡驱动问题",
        "policy",
        "gpu_acceleration",
    ),
    ComponentDefinition(
        "storage",
        "本地存储服务",
        "负责缓存、IndexedDB 与本地会话数据",
        "required",
    ),
    ComponentDefinition(
        "crash_handler",
        "崩溃恢复服务",
        "收集故障信息并协助异常恢复",
        "observe",
    ),
    ComponentDefinition(
        "helper",
        "系统辅助进程",
        "音频、Node.js 或其他由应用按需维护的服务",
        "observe",
    ),
    ComponentDefinition(
        "unknown",
        "未分类进程",
        "新版应用产生的未知角色，仅观察且绝不自动终止",
        "observe",
    ),
)


APP_COMPONENTS: Dict[str, Tuple[ComponentDefinition, ...]] = {
    "antigravity": _COMMON_COMPONENTS,
    "codex": (
        _COMMON_COMPONENTS[0],
        ComponentDefinition(
            "app_server",
            "Codex API 服务",
            "负责对话、工具调用与 Responses WebSocket 通信",
            "required",
        ),
        *_COMMON_COMPONENTS[1:],
    ),
}


def get_component_definitions(app_id: str) -> Tuple[ComponentDefinition, ...]:
    return APP_COMPONENTS.get(app_id, _COMMON_COMPONENTS)
