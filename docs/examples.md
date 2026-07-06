# 端到端示例（Claude / Cherry Studio 会话）

下面是一些真实场景里 Agent 调 k8s-mcp 工具的对话片段。每个例子是**完整的
"用户问 → Agent 调"**链条，便于复刻到自己的会话里。

> 工具调用语法是 MCP 标准的——Agent 拿到 `tool_name(arg=value, ...)` 的
> schema，自己选 arg 怎么传。Cherry Studio + qwen3-max / Claude Desktop +
> Sonnet 等都遵循同一套。

---

## 1. 新会话开头

> 你："连上 prod 帮我看看。"

> Claude → `cluster_info()` 拿 apiserver / 版本 / 计数 →
> `whoami(namespace="prod")` 拿身份和有效权限 →
> 据此判断能做什么。

---

## 2. 起一个工作负载并暴露

> 你："部署 nginx 1.25，Deployment 3 副本，再加 Service 和 Ingress 暴露。"

> Claude → `create_deployment`, `expose_workload`, `create_ingress`。

---

## 3. 排障：错误日志

> 你："找出最近一小时所有 5xx 错误。"

> Claude → `get_pod_logs(label_selector=app=nginx,
> pattern=r"\b5\d\d\b", context_lines=2, since_seconds=3600)`。

---

## 4. 排障：HPA 状态

> 你："给我看看 HPA 的当前副本数。"

> Claude → `get_resource_jsonpath("HorizontalPodAutoscaler",
> "status.currentMetrics", name="web", namespace="default")`。

---

## 5. 升级影响面

> 你："还有谁在用 nginx:1.21？我想升级影响面看清楚。"

> Claude → `find_images("nginx:1.21")` → 一张表列出所有引用 1.21 的
> Deployment / StatefulSet / DaemonSet 及其容器。

---

## 6. 排障：单对象事件

> 你："api-1 起来了吗？给我看相关事件。"

> Claude → `get_events_for_object(kind="Pod", name="api-1", namespace="prod")`
> → 拿到该 Pod 的所有 Warning / Normal 事件按时间倒序。

---

## 7. 一次性任务（Job）

> 你："跑一个 DB 迁移任务，image 用 postgres:16-alpine，命令 pg_dump。"

> Claude → `create_job(name="migrate-2026-07-03", image="postgres:16-alpine",
> namespace="db", command=["pg_dump", "-U", "postgres"],
> env={"PGHOST": "db"}, backoff_limit=2)`。

---

## 8. 周期任务（CronJob）

> 你："每天凌晨 2 点清一次临时表，搞成定时任务。"

> Claude → `create_cronjob(name="tidy-temp", image="alpine:3",
> schedule="0 2 * * *",
> command=["sh", "-c", "psql ... -c 'TRUNCATE temp_events'"])`。

---

## 9. 升级镜像

> 你："等 Deployment rollout 完成，然后把镜像升到 1.27。"

> Claude → `wait_resource("Deployment", "nginx", namespace="default",
> for_condition="Available")` → `set_image(...)`。

---

## 10. 节点维护

> 你："drain node-3，我要重启它。"

> Claude → `cordon_node("node-3")` → 列 Pod → `drain_node("node-3")`。

---

## 11. Prometheus 桥接 + Pod 指标

> 你："看一下 api-1 现在的 CPU 和内存。"

> Claude → `find_prometheus_service()` → RECOMMENDED 列读出
> `expose_prometheus_as_nodeport(namespace='default',
> service_name='monitor-kube-prometheus-st-prometheus')` 照抄调用 →
> 拿到 `node_port=31245` → `list_resources(kind='Node')` 拿节点 IP
> `10.20.30.40` → `pod_metrics("api-1", "default", "cpu",
> prometheus_url="http://10.20.30.40:31245")` →
> `pod_metrics("api-1", "default", "memory",
> prometheus_url="http://10.20.30.40:31245")`。

---

## 12. 批量清孤儿 PVC

> 你："把 prod namespace 里所有 `app=db` 标签的孤儿 PVC 清掉。"

> Claude → `list_resources(kind="PersistentVolumeClaim", namespace="prod", label_selector="app=db")` 列出所有匹配项 →
> 用户逐个（或一次性脚本）确认 → 逐个调 `delete_resource(kind="PersistentVolumeClaim", name=..., namespace="prod")`。

---

## 13. 删除任意资源（v0.5.2 起单步）

> 你："把它删了。"

> Claude → 调 `delete_resource(kind="Pod", name="api-1", namespace="default")` 直接执行（受 `READ_ONLY` + `NAMESPACE_ALLOWLIST` 守门）。如需先看预览再确认，Agent 可以先 `get_resource_yaml` 给用户看一眼再删——确认机制由 agent + 用户决定，不再由工具强制。
