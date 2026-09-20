# data_processing

MCAP 预处理脚本运行环境（mcap 质检 `mcap_precheck`、通用日志 `logger` 等）。

依赖与运行时配置统一由 `pyproject.toml` + `uv.lock` 管理，**虚拟环境 `.venv` 不纳入版本控制**（已在根 `.gitignore` 忽略），请按下方步骤自行生成。

## 环境准备

要求 Python >= 3.10。推荐使用 [`uv`](https://docs.astral.sh/uv/) 管理依赖。

### 方式一：uv（推荐，与仓库锁文件一致）

```bash
cd data_processing
uv venv                 # 生成 .venv
uv sync                 # 按 uv.lock 安装默认依赖组（mcap）
```

相机类 h264/h265 解码（非必需）单独成组，需要时再加：

```bash
uv sync --group camera  # 额外安装 av
```

### 方式二：标准 venv + pip

```bash
cd data_processing
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install "mcap>=1.2.0" "mcap-ros2-support>=0.5.0" "pillow>=10.0.0" "numpy>=1.24.0" "pyyaml>=6.0.0"
# 相机解码可选：pip install "av>=13.0.0"
```

> 依赖版本以 `pyproject.toml` 的 `[dependency-groups]` 为准，优先用 uv 保持锁版本一致。

## 运行与测试

```bash
source .venv/bin/activate
pytest                       # 已配置 pythonpath=src，可直接 import src 下模块
python -m src.mcap_precheck --help
```
