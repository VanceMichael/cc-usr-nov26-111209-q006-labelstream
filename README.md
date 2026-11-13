# labelstream

物流标签 ZPL 作业的指令边界检查与原始作业归档。

## 现有能力

inspect 查看一份源文件的结构；capture 发起一次本机 HTTP 请求并保存响应状态、原始字节和摘要；show 列出接收记录。原文对象按 SHA-256 共用，独立请求分别记账。错误 HTTP 状态也会保存，原文归档不等于业务接收完成。

extract 持续接收打印网关的分块作业，把每次实际打印的标签采成结构化记录：字段文本、字段编号及位置、条码载荷、份数和当时的格式版本，可追溯到产生这张标签的作业字节位置。进程可停在转义或图形块中间，续收同一 streamId 后从已确认水位继续；重放已确认字节不会重复计份。未定义变量、递归格式和损坏的作业被明确隔离，不污染相邻标签。jobs 列出作业，labels 按作业查询打印记录，isolated 查看隔离解释。inspect 仍只做词法观察，不展开格式。

## 运行

使用 Python 3.11 或更新版本，仅依赖标准库，无安装步骤。全部示例均为本地合成资料，不包含真实个人信息、商业凭据或线上地址。

```sh
python3 -m labelstream inspect labelstream/fixtures/sample.zpl
python3 -m labelstream demo
python3 -m unittest discover -s tests -v
python3 -m compileall -q labelstream
```

需要手动发请求时运行 `python3 -m labelstream.source_server --port 8765`，再在另一终端运行 `python3 -m labelstream capture http://127.0.0.1:8765/job`。默认会在当前目录创建 captures.sqlite3 和 objects；可通过 --db 和 --objects 指定独立位置。演示自己启动源服务，使用随机本机端口，并在退出时清理临时目录。

持续接收一个作业流并查询结果（/stream 支持 name 选择演练流，分块大小 --chunk 最大 4096）：

```sh
python3 -m labelstream extract --url 'http://127.0.0.1:8765/stream?name=sample' --chunk 64
python3 -m labelstream jobs
python3 -m labelstream labels --job print-gateway-sample
python3 -m labelstream isolated --job print-gateway-recursive
```

demo 会演示格式重定义前后同一个变量值 LOT-7 打印出不同固定文本（批次 → 温控批次，格式版本 1 → 2）的结果。

## 模块

`labelstream/parser.py` 负责单文档观察，`archive.py` 保存不可变原文与请求事实，`extract.py` 把确认字节解释成结构化标签记录并按作业持久化，`transport.py` 限制实验请求到本机，`source_server.py` 提供来源示例。来源边界及字段含义见 `docs/source-contract.md`。这些入口不会声称一次响应已经构成完整目录或业务批次。

## 容器

Dockerfile 使用 Python 3.11 标准库镜像，不需要应用依赖下载。可用 `docker build -t labelstream:local .` 构建，再执行 `docker run --rm --network none labelstream:local`。本地 HTTP 与 PTY 演练不需要外部网络，测试及单次演示通常数秒完成。
