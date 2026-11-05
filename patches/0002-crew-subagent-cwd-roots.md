# 0002 — crew: sub-agent `cwd` under a drive root

**Target**: `C:\Program Files\KiroCrew\resources\backend-dist\kirocrew-backend\Lib\site-packages\kiro_crew\subagent_parts\agent_policy.py`
**Applied**: 2026-11-05 (KiroCrew install of 10-31)
**Backup**: `%LOCALAPPDATA%\kirocrew-openai-bridge\backups\agent_policy.py.orig` (and the stale `.pyc` beside it)

## Two defects, one refusal

1. `import os` sat inside `if TYPE_CHECKING:` — so at runtime `validate_cwd()`
   raised `NameError: name 'os' is not defined` for ANY non-empty `cwd`. The
   module is only correct for a type checker, never for a running gateway.
2. `os.path.realpath("D:/")` is `"D:\\"` — a drive root keeps its separator.
   Comparing against `root + os.sep` therefore asked for `"D:\\\\"`, and every
   path under the drive was refused with
   `cwd is not under any allowed root: ['~/workspace', ..., 'D:/', ...]`.

Observed live: `Subagent spawn refused: invalid cwd 'D:\租界\一号大楼\项目开发'`
(2026-11-05 12:09:54, twice), and sub-agents that DID start ran in the KiroCrew
install directory, so project tests could not run at all.

## Patch

- `import os` moved to module level.
- Each resolved root is normalised once with `root.rstrip("\\/") or root`
  before the prefix comparison.

## Verify

```powershell
& "C:\Program Files\KiroCrew\resources\backend-dist\kirocrew-backend\python.exe" -s -P -c @"
from kiro_crew.subagent_parts.agent_policy import validate_cwd
roots=['~/workspace','~/workspaces','~/workplace','~/workplaces','D:/','C:/Users/Administrator','E:/']
for t in [r'D:\租界\一号大楼\项目开发', r'D:\租界\一号大楼\项目开发\编码区', r'C:\Users\Administrator\Desktop', r'E:\ZCODE项目处理', '', 'relative/path', r'F:\nope']:
    print(repr(t), '->', validate_cwd(t, roots))
"@
```

Expected after the patch (measured):

```
'D:\租界\一号大楼\项目开发'        -> ('D:\租界\一号大楼\项目开发', '')
'D:\租界\一号大楼\项目开发\编码区' -> ('D:\租界\一号大楼\项目开发\编码区', '')
'C:\Users\Administrator\Desktop'  -> ('C:\Users\Administrator\Desktop', '')
'E:\ZCODE项目处理'                 -> ('E:\ZCODE项目处理', '')
''                                -> ('', '')
'relative/path'                   -> ('', 'cwd must be an absolute path')
'F:\nope'                         -> ('', 'cwd does not exist or is not a directory')
```

## Re-apply after a KiroCrew upgrade

The install directory is replaced on update. Re-apply by hand (two edits above),
then **delete the stale bytecode**, or the fix will not load:

```powershell
Remove-Item "...\kiro_crew\subagent_parts\__pycache__\agent_policy.cpython-312.pyc" -Force
```

The shipped `.pyc` is hash-pinned, so a source edit alone is ignored by the
interpreter — this cost one debugging round here.
