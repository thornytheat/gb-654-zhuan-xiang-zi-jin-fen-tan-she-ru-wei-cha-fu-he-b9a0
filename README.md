# 专项资金分摊与舍入尾差复核

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 预算采信：`/api/budgets/import`
- M2 资格校验：`/api/eligibility/evaluate`
- M3 权重测算：`/api/weights/calculate`
- M4 金额分摊：`/api/allocations/calculate`
- M5 尾差裁决：`/api/remainders/resolve`
- M6 批次复核：`/api/export/allocations.csv`
- M7 财政报送：`/api/reports` · `/api/reports/contract/switch` · `/api/receiver/inbox`

        ## 核心规则

        先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。

        ## 财政报送契约换版

        旧契约 v1 的收款单位是一个合并栏目 `payee`；新契约 v2 拆成法人 `legal_entity`
        与分支 `branch`，缺分支拒收。首次报送在同一事务内固定契约版本与原文，此后
        原文不可改写。换版只影响之后的新报送：失联旧报送仍按 v1 原文找回回执，不补
        新栏。切版与新建并发时以写事务内复核为准，过期版本返回 `STALE_VERSION`。

        回执丢失时先查询本地接收端，查到即绑定、不重投；确认未收件才按原内容重试，
        接收端按报送编号幂等收件。发送中断后状态留在库中，可调用恢复入口续跑。
        本地接收端仅模拟收件，不连接真实财政系统。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18213`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

财政报送样例：`sample_data/report_legacy.json`（v1）与 `sample_data/report_split.json`（v2）。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。
