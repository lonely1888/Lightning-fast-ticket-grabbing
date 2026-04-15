# 12306 抢票桌面助手

一个基于 Python 的 12306 抢票项目，包含桌面界面、Cookie 登录、车次查询、多段行程选择和自动下单流程。

当前仓库已经做过基础整理：

- `config.yaml` 保持为模板内容，不包含个人信息
- `cookies.txt` 默认为空文件，并已加入 `.gitignore`
- 日志、调试输出、打包产物不会提交到 GitHub

## 功能概览

- PyQt5 桌面界面，按步骤完成登录、乘客管理、选票和抢票
- 支持账号密码 + 短信验证码登录
- 支持直接粘贴 Cookie 并校验有效性
- 支持查询车次、价格、余票和候选座席
- 支持首选 / 保底车次配置
- 支持中转段配置与连续抢票流程
- 命令行模式下可按时间阶段动态调整查询频率

## 项目结构

```text
.
├── app.py                # 桌面版入口（PyQt5）
├── main.py               # 命令行入口
├── login.py              # 登录、Cookie、短信验证码相关逻辑
├── query.py              # 车次查询、站点匹配、配置读写
├── order.py              # 下单与排队逻辑
├── paths.py              # 运行目录 / 资源目录定位
├── station_name.js       # 12306 站点数据
├── config.yaml           # 配置模板
├── cookies.txt           # Cookie 文件（默认空）
├── app_icon.ico          # 桌面图标
└── 超级nb的抢票助手.spec  # PyInstaller 打包配置
```

## 运行环境

- Windows
- Python 3.10 及以上

建议先创建虚拟环境，再安装依赖。

## 安装依赖

```bash
pip install requests schedule loguru pyyaml ddddocr gmssl PyQt5 pyinstaller
```

如果你只运行脚本、不打包桌面程序，`pyinstaller` 可以不装。

## 配置说明

项目默认使用模板化的 `config.yaml`。首次使用前，请按自己的行程补全配置。

当前模板示例：

```yaml
query:
  from_station: "XXX"
  from_station_name: "出发城市"
  to_station: "XXX"
  to_station_name: "目的城市"
  train_date: "2026-01-01"
  target_depart_time: ""
  passenger_count: 1
  seat_preference:
    - "二等座"
    - "一等座"
passengers: []
segments:
  - from_station: "XXX"
    from_station_name: "出发城市"
    to_station: "XXX"
    to_station_name: "目的城市"
    train_date: "2026-01-01"
    primary:
      train_code: ""
      depart_time: ""
      arrive_time: ""
      seat_name: ""
    backup:
      train_code: ""
      depart_time: ""
      arrive_time: ""
      seat_name: ""
selection:
  primary:
    train_code: ""
    depart_time: ""
    arrive_time: ""
    seat_name: ""
  backup:
    train_code: ""
    depart_time: ""
    arrive_time: ""
    seat_name: ""
schedule:
  enabled: true
  interval_seconds: 4
```

说明：

- `passengers` 需要按实际乘车人填写
- `segments` 用于多段行程或中转配置
- `selection` 用于保存当前选中的首选 / 保底车次
- `cookies.txt` 可由程序登录后自动写入，也可以手动粘贴 Cookie

## 使用方式

### 1. 启动桌面版

```bash
python app.py
```

桌面版包含四个主要步骤：

1. 登录
2. 乘客管理
3. 选票
4. 抢票

### 2. 启动命令行版

```bash
python main.py
```

命令行版会读取本地 `config.yaml` 和 `cookies.txt`，并按程序内置的分时段策略持续查询和尝试下单。

## 打包

如果你想打包桌面程序，可以使用仓库内的 `.spec` 文件：

```bash
pyinstaller "超级nb的抢票助手.spec"
```

打包输出目录 `dist/` 已加入 `.gitignore`，不会上传到 GitHub。

## 文件与隐私说明

为了避免把个人信息误传到仓库，当前仓库默认忽略以下内容：

- `cookies.txt`
- `dist/`
- 各类 `.log` 日志文件
- 登录调试输出文件

如果你准备公开仓库，建议提交前再次确认：

- `config.yaml` 中没有真实姓名或身份证号
- `cookies.txt` 为空或未被纳入版本控制
- 日志和截图中没有敏感信息

## 说明

这个项目更适合作为个人学习、流程验证和桌面工具开发参考。12306 接口、登录校验和风控策略可能随时间变化，实际运行时请以最新行为为准。
