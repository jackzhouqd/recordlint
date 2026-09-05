"""本地审核服务的 Web 层。

拆包原因：原 server.py 已 831 行，还要再塞进训练工作台。现在按
「路由分发（app）/ 骨架与组件（render）/ 页面（views）」三层切开，
每个视图只关心取什么数据，样式与骨架不重复。
"""
from .app import Config, Handler, evidence_jpeg, serve  # noqa: F401
