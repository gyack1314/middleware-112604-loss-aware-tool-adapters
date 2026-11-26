# 工具路由的转换预算与上游协商

unknown 与 absent 是独立区分。转换成本使用整数，申请可设置 budget。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **adapter**　edge、input/output、keeps、cost 与 active。 示例中的成本只用于适配路径，旧执行的在途成本仍需真实回执。
- **tool**　tool、source、result、available。
- **query**　id 的 source/target，need 列不可丢区分，budget 是转换成本上限。
- **accepted**　工具以 attempt 确认接受 id。
- **reply**　attempt 回报 done、unknown 或 never。
- **withdraw**　禁用 edge。
- **restart**　重启。
- **inspect**　查询 id。

## 示例

- compatible-summary-not-citation.jsonl
- retired-edge-with-live-tool.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的输入。
