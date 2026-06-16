# Pro 视频去水印常驻服务（ProPainter）

去掉 Gemini/Flow Pro 账号视频右下角的 sparkle（✦）水印。基于 ProPainter 视频时序
inpainting，模型常驻显存，本机 HTTP 调用。flow2api 在 Pro 视频生成后调用本服务。

- 输入/输出：本地文件路径（与 flow2api 同机，共享文件系统）。
- 性能（RTX 2080Ti，模型常驻）：4s 视频 ~5.5s / 10s 视频 ~13s。
- 质量：4× 像素级放大无星/无补丁/无糊，时序一致（已大屏验收）。

## 一次性安装

> 用一个带 torch+CUDA 的 Python 环境运行（本项目复用 ComfyUI 的 venv：
> `/opt/ComfyUI/.venv/bin/python`）。

1. **克隆 ProPainter 并下权重**
   ```bash
   git clone https://github.com/sczhou/ProPainter.git /opt/propainter
   cd /opt/propainter/weights
   for w in raft-things.pth recurrent_flow_completion.pth ProPainter.pth; do
     curl -L -o $w "https://github.com/sczhou/ProPainter/releases/download/v0.1.0/$w"
   done
   ```

2. **把 ProPainter 的推理脚本包成可调用的 `main()`**（本服务需要 import 调用）。
   `inference_propainter.py` 原本把整段推理写在 `if __name__ == '__main__':` 里。改成：
   - 将第一行 `if __name__ == '__main__':` 改为 `def main():`（缩进不变）；
   - 文件末尾追加：
     ```python
     if __name__ == '__main__':
         main()
     ```

3. **装缺失依赖到该 venv**
   ```bash
   uv pip install --python /opt/ComfyUI/.venv/bin/python imageio-ffmpeg
   # 其余依赖(av/addict/einops/scipy/opencv/scikit-image/timm/torch/torchvision...)
   # ComfyUI venv 一般已具备；缺啥补啥。
   ```

4. **系统需有 `ffmpeg`**（裁剪与合成用）。

## 运行

```bash
PROPAINTER_DIR=/opt/propainter \
/opt/ComfyUI/.venv/bin/python /opt/Projects/flow2api/dewatermark/server.py
```

常驻部署用 systemd：见同目录 `flow2api-dewatermark.service`（按需改路径），然后
```bash
sudo cp flow2api-dewatermark.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now flow2api-dewatermark
```

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROPAINTER_DIR` | （必填） | ProPainter 仓库目录（已包 main()，含 weights/） |
| `WM_MASK_DIR` | `<本目录>/masks` | sparkle 蒙版目录（mask192.png / mask192_alpha.png） |
| `WM_PORT` | `18290` | 监听端口（仅 127.0.0.1） |
| `WM_CROP` | `192,192,1064,504` | 720p 水印裁剪框 W,H,X,Y（水印固定 (1136,576)/48px） |
| `WM_RAFT_ITER` | `12` | RAFT 迭代 |
| `WM_CRF` | `14` | 输出 x264 CRF（视觉无损） |

## API

```bash
curl http://127.0.0.1:18290/health
# {"ok": true, "models_loaded": ["flow","pp","raft"]}

curl -X POST http://127.0.0.1:18290/dewatermark \
  -d '{"input":"/abs/in.mp4","output":"/abs/out.mp4"}'
# {"ok": true, "output":"...", "timings": {...}, "total": 12.9}
```

- GPU 串行（内部锁），一次处理一条。
- 仅覆盖 720p sparkle 水印；其它分辨率/无水印应由调用方（flow2api）按 tier 判断后再决定是否调用。

## 适用范围

- **做**：720p Pro 视频（t2v/r2v/i2v，横竖屏）去可见 sparkle。
- **不做**：SynthID 隐形水印（视频不可行）；1080p（位置/尺寸需另标）；图片（无水印）。
