# Python Ticket Project

一个用于抢票流程开发的基础 Python 项目骨架，包含登录、查询、下单三个模块，以及配置、日志和定时任务入口。

## 项目结构

```text
.
├── main.py
├── login.py
├── query.py
├── order.py
├── config.yaml
└── README.md
```

## 已使用依赖

- requests
- schedule
- loguru
- pyyaml
- ddddocr

## 运行方式

1. 按需修改 `config.yaml`
2. 运行：

```bash
python main.py
```

## 模块说明

- `main.py`：程序入口，负责加载配置、初始化日志、控制执行流程
- `login.py`：登录与验证码识别预留
- `query.py`：查询票源逻辑预留
- `order.py`：下单逻辑预留

## 后续建议

- 在 `login.py` 中接入真实登录接口
- 在 `query.py` 中解析票源数据
- 在 `order.py` 中实现提交订单与重试机制
