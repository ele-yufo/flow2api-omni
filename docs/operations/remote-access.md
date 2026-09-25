# 跨网访问（Tailscale）

2026-09-06 起，不在 LAN-124 的机器通过 Tailscale 访问本服务，**地址始终是
`http://192.168.124.151:18282`**——LAN-124 是主力服务网，所有客户端配置都按它写，
在家直连、在外经 Tailscale 子网路由，配置不用改。同日曾短暂上线过
`flow.ele-yufo.com`（ECS-BJ → JD frps → frpc），因为吞吐只有 0.4MB/s 已拆除。

2080TI 的 tailnet 地址 `100.125.44.42:18282` 等价可用，但只在需要绕开子网路由时才用
（例如某台机器自己就在 LAN-124 里）。

## 拓扑

2080TI 是 Tailscale 子网路由，广播 `192.168.124.0/24`：

```text
Mac（在外） / R7H255 / ZFlow13
→ tailscale（点对点，IPv6 直连）
→ 2080TI(subnet router) → 192.168.124.0/24
  ├─ 192.168.124.151:18282  flow2api
  ├─ 192.168.124.151:8000   智人Beta 素材系统
  ├─ 192.168.124.151:8001   视频管线素材服务
  ├─ 192.168.124.150:8188   ComfyUI（4090D）
  └─ 192.168.124.240        NAS
```

Linux 节点必须 `tailscale set --accept-routes` 才会用这条子网路由；macOS 默认就是接受的。

**在 192.168.124.0/24 里的机器不能接受这条路由。** 2026-09-06 在 4090D 上实测：打开
`--accept-routes` 后它把发往本网段的包丢进隧道，自己的 LAN 立刻失联（ping、SSH 全断），
只能从 tailnet 地址进去关掉才恢复。所以：

- **4090D 永久 `--accept-routes=false`**（它常驻 LAN-124）。
- **MacBook 装了自动开关**，不需要人工记忆：`~/.local/bin/tailscale-subnet-guard.sh`
  由 LaunchAgent `com.yufo.tailscale-subnet-guard` 每分钟（以及网络变化时）执行，
  检测到本机拿到 `192.168.124.x` 就 `--accept-routes=false`，离开自动恢复 true；
  Tailscale 可以一直开着。判据只看本机地址，不看 SSID，换网线换 WiFi 都成立。
  日志在 `~/Library/Logs/tailscale-subnet-guard.log`，怀疑它没生效先看这里。
- macOS 上执行 `tailscale set` 不要加 sudo，加了会写到 root 的 profile，静默不生效。
- R7H255 / ZFlow13 常驻 LAN-0，与被广播的网段不重叠，`--accept-routes` 一直开着即可。

## 直连依赖 IPv6，不是可选项

家宽在运营商侧还有一层 NAT：路由器 UPnP 映射了 UDP 41641，从公网打进来的探针
**一个都收不到**，所以 IPv4 打洞不可能。可用的直连路径是 IPv6：

- 光猫/路由器必须开 IPv6（2026-09-06 开通），2080TI 需要 `accept_ra=2`
  （开了转发的机器 `accept_ra=1` 会被内核忽略），已写进 `/etc/sysctl.d/99-tailscale.conf`；
  NetworkManager 拉起网卡时会重置该值，所以另有一道
  `/etc/NetworkManager/dispatcher.d/50-tailscale-ipv6` 每次 up 补设。
- 验证直连：`tailscale ping macbook-pro` 显示 `via [240e:...]:41641` 才算直连，
  出现 `via DERP(...)` 说明退回中继，吞吐会掉一个数量级。

实测：直连 4.8MB/s（Mac）、0.95MB/s（R7H255）；退回中继时 0.15MB/s；
被拆掉的公网入口是 0.4MB/s。

## mihomo 共存（两个坑）

- **fake-ip 会毒死登录**：mihomo 把 `controlplane.tailscale.com` 解析成 198.18.x，
  而 tailscaled 的包带 fwmark `0x80000` 走 main 表绕开 mihomo → 连不上假 IP。
  解法是让 tailscaled 走本机代理，见 `/etc/systemd/system/tailscaled.service.d/proxy.conf`。
- **数据面不受影响**：`ip rule` 里 tailscale 的 5210 优先于 mihomo 的 9000+，
  带 mark 的流量直接走 enp3s0。IPv6 侧 mihomo 有一条 `9000: from all unreachable`，
  普通程序的 IPv6 是不通的，但 tailscale 的标记流量不受它管——别据此以为 IPv6 坏了。

## 产物仍由服务端中转

`cache_config.cache_enabled=1` 保持不变：客户端都在国内、没有 Google 出口，
产物由 2080TI 下载后以 `<入口>/tmp/<md5>` 返回，保留 6h。返回域名由请求头推导，
`cache_base_url` 继续留空。产物 URL 不带鉴权（capability URL），别外传。
