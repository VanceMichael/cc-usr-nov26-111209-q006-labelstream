# 标签网关作业子集

GET /job 为完整合成作业，/stream?offset=0&limit=17 返回 JSON，包含 start/end、base64、eof、streamId。offset 是不可变 streamId 中的字节位置，end 为不包含端点。limit 最大4096，响应块可落在任意UTF-8或指令内部。只有确认已持久化字节才能续用 end；重请求同一区间返回相同字节。

受支持 ZPL 子集：^XA/^XZ 标签定界；^DFname^FS 在本标签存储格式，不产生打印事件；^XFname^FS 调用当时已定义的格式；^FN整数 与后续 ^FD..^FS 绑定编号；^FOx,y 指定字段位置；^BC/^B3 将下一字段解释为条码载荷；^PQ整数（1-1000）指定份数，默认1；^CI28 为UTF-8，^CI0 为cp437；^FH后一个字节为当前字段的十六进制转义引导符，未指定用下划线；^GFB,total,used,rowBytes, 后紧接 total 字节不透明图形，随后是 ^FS。项目子集限定 total=used，图形内容可能出现 ^XA 或 ^FS 字节。未声明指令须记录不支持原因。

字段从 ^FD 到未转义 ^FS；顺序和重复编号保留。格式引用可嵌套但不允许循环，限制展开深度16与每标签字段数10000。调用未给必需 ^FN 值是未绑定，不使用上一次打印的值。格式更新仅作用于随后作业，^DF 本身不计份数。词法观察器只列出候选前缀，不执行格式展开，不把候选前缀声称为已解释的业务标签。

## extract 记录约定

extract 按作业（url + streamId）把确认字节解释成事件，事件在作业内按 seq 单调编号；同一确认字节区间重放得到相同 seq，重复接收或进程重启不重复计份。进程可停在指令、转义或图形块中间，续收同一 streamId 后从已确认水位继续；streamId 变化视为来源错误。块与事件在同一事务落盘。

事件 kind：
- label：一次实际打印。copies 为 ^PQ 份数（默认1）；fields 按出现顺序，含 n（^FN 编号或 null）、x/y（^FO 或 null）、text（按当时 ^CI 解码）、barcode（^BC/^B3 或 null）、offset（^FD 在作业中的字节位置）；formats 按展开顺序记录 name+version；graphics 记录 ^GF 数据位置与字节数；notes 记录不支持原因（unsupported-command、unsupported-ci、invalid-fo、invalid-fh、gf-total-used-mismatch、fn-without-field、unused-binding）。不推断哪个字段是批次号。
- format：^DF 存储的格式版本（name+version），不计份数，不产生 label。
- isolated：被隔离的损坏或不可解释内容，其字段不进入任何 label，后续标签独立解释。reason 取值：nested-xa、unterminated-label、xz-outside-label、token-outside-label、truncated-command、truncated-field、truncated-escape、truncated-graphic、bad-command、bad-hex-escape、graphic-missing-fs、unsupported-gf-compression、bad-gf-header、multiple-df、bad-format-name、undefined-format、recursive-format、depth-exceeded、unbound-variable、invalid-fn、invalid-pq、decode-error、too-many-fields。
