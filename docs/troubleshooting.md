# 排错与开发场景

dev / test 集群上踩到的几个常见坑，都给了**一次性工具**。

---

## 集群没有 StorageClass？dev/test 一键装 local-path

kind / k3s 默认 / minikube（没装 extra）这些场景下，集群**根本没有
StorageClass**，PVC 提交即 Pending。`bootstrap_local_path_provisioner`
一次解决：

```
bootstrap_local_path_provisioner()      # 应用 Rancher local-path-storage
```

装好后 `storage_class_name="local-path"` 立刻可用，PVC 提交即自动
创建 hostPath PV。**生产环境不要用**（hostPath 不抗节点故障，数据随节点死亡丢）。

参数：

- `set_as_default=True`（默认）—— 把新建的 SC 标为集群默认，后续 PVC 不写
  `storage_class` 也行。
- `apply_immediately=False` —— 只返回 manifest YAML，先看一眼再装（适合审计）。
- `K8S_MCP_LOCAL_PATH_PROVISIONER_URL` —— 离线 / 内网集群，指向你自家的镜像；
  默认指向 [Rancher 官方 manifest](https://raw.githubusercontent.com/rancher/local-path-provisioner/master/deploy/local-path-storage.yaml)。

Manifest 在 session 内 fetch + cache 一次（每次 MCP 重连会重拉）。

---

## Pod 一直 FailedMount？hostPath 主机的目录可能没建

dev / test 集群常用手搓的 **hostPath PV**（`spec.hostPath.path=/data/xxx`），
但 kubelet **不会**自动在节点上建这个目录。Pod 卡在 ContainerCreating，
事件里看到：

```
Warning  FailedMount  ... path "/data/k8s/pgsql-sts" does not exist
```

处理流：

1. `validate_pv_hostpath_paths()` —— 列出所有 hostPath PV、对应的节点、
   主机路径，**直接给出一行可复制的 `ssh` 命令**（先 `ls -ld` 检查，
   缺则 `sudo mkdir -p`）。
2. 修好后 Pod 会自动重试挂载。
3. `create_pvc(volume_name="...")` 在绑定的 PV 是 hostPath 时，**返回里
   会自动带 `mkdir -p` 提示**，避免下次再踩坑。

PVC 想绑到具体 hostPath PV 必须显式 `volume_name`（PVC 没有 SC 的情况下，
k8s 不会自动按 hostPath path 匹配）。

---

## 写工具返回 `Forbidden`？

调 `whoami(namespace="<目标 ns>")` 看身份 + 有效权限——能直接定位是 SA
权限不够还是 namespace 选错。详见 [tools.md → `whoami`](./tools.md#whominamespacedefault)。

---

## 集群里找不到 Prometheus？

1. `find_prometheus_service(namespace=None)` 先扫一遍。
2. 看 `RECOMMENDED` 列的字面签名，照抄下一步调用。
3. 三种 TYPE 三种走法（NodePort / ClusterIP / ClusterIP 不可路由）详见
   [tools.md → Prometheus 端点发现 + 桥接协议](./tools.md#prometheus-工具prometheus_query--prometheus_query_range--pod_metrics)。

---

## MCP server 看不到新的 tools？

MCP server 是常驻进程，**代码改动要重启 server 才生效**。多数 Agent
（Cherry Studio / Claude Desktop）的 UI 重启**不会**重启 MCP server；
要看 MCP server 是否在跑新代码，**MCP 客户端连接重连**（删了再加）即可。

详见顶层 README 的「更新」说明。
