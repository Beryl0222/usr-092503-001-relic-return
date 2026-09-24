# 流失文物返还协作台账

本项目用于保存跨机构文物返还案件的基础数据约定。仓库当前提供案件阶段、参与职责和材料类别等领域契约，后续服务可在这些稳定标识之上实现流程、审计与对外接口。

## 目录

- `src/relic_case/contracts.py`：案件领域枚举与输入校验。
- `tests/`：基础契约测试。

## 运行

执行测试：

```bash
python3 -m unittest discover -s tests -v
```

执行构建检查：

```bash
python3 -m compileall -q src
```
