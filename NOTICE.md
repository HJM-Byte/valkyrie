# 第三方依赖与不随仓库分发的内容

本仓库的 **代码** 使用 MIT 许可证（见 `LICENSE`）。下面这些内容不因仓库的 MIT 许可证而改变授权，也不在本仓库中分发。

## Python 依赖

`requirements*.txt` 中列出的包由各自作者维护，使用各自的许可证。安装前请阅读对应项目的许可证，尤其是：

- `faster-whisper` / CTranslate2
- `sherpa-onnx`
- `onnxruntime`
- `opencv-python`
- `edge-tts`
- `pygame`
- `sounddevice` / `soundfile`
- `Pillow`

## 模型权重

以下模型 **不在本仓库中**，需要用户自行下载并遵守上游许可证：

- OpenCV YuNet 人脸检测模型
- OpenCV SFace 人脸特征模型
- `Systran/faster-whisper-small` 语音识别模型
- 本地 VITS 中文声线（AISHELL-3 训练）
- YOLOv8 / 物体检测模型（若启用）

`download_faces.py` 与 `download_whisper.py` 只是下载脚本，不代表授权。特别是 VITS 声线：上游模型与训练数据的授权需要在对外分发或商用前单独确认，不能因为本仓库是 MIT 就默认可以再分发。

## 语音与云服务

- BreezeBlue 音色与 `breeze-tts-2` 接口由服务提供方授权；仓库只包含调用代码，不包含音色样本或账号。
- 阿里云百炼 / Qwen 的模型与 API 受服务条款约束；仓库不包含 Key。
- 任何 API Key、账号、手机号、局域网地址都不应提交。

## 人格文本与素材

`PERSONA.md` 是默认人格模板，使用本仓库的 MIT 许可证。你可以把它改成自己的版本。

`app/pocket_web/icons/`、`hardware/` 下的图片和图纸用于界面与硬件说明；如果你准备再分发，请先确认这些素材的来源与授权。无法确认来源的素材不要放进公开仓库。

## 个人数据

以下内容被 `.gitignore` 排除，也不应通过 Issue、截图或压缩包公开：

- `data/`：记忆库、对话、日记、人脸底库、日志、截图
- `diary/`
- `backups/`
- `models/`
- `docs/`：本地开发记录
- `.env`、`data/breeze_key.txt`、`data/pocket_token.txt`
- `app/pocket_apk/keystore/` 与构建出的 APK
