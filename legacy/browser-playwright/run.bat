@echo off
REM ===========================================================================
REM  芯位蜜线自动学习工具 —— Windows 启动器
REM
REM  为什么需要这个 bat：
REM    Windows 中文控制台默认代码页是 GBK(936)，而本程序大量使用中文和
REM    ✅⚠⏳ 等符号，直接运行 python main.py 会出现乱码甚至
REM    "UnicodeEncodeError: 'gbk' codec can't encode character" 报错。
REM    这里先把代码页切成 UTF-8(65001) 再启动 Python，中文即可正常显示。
REM
REM  用法（和 python main.py 完全一致，把 python main.py 换成 run.bat）：
REM      run.bat check
REM      run.bat login --user 138xxxxxxxx
REM      run.bat scan  --course https://www.beeline-ai.com/xxx/course/123
REM      run.bat run   --course 123
REM ===========================================================================

chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM 让 Python 自身也按 UTF-8 处理标准流
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM 如果还没设过密码，可以在这里取消注释并填写（不推荐：明文会留在文件里）
REM set BEELINE_USER=138xxxxxxxx
REM set BEELINE_PASS=你的密码

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 main.py %*
) else (
    python main.py %*
)

set EXITCODE=%errorlevel%
endlocal & exit /b %EXITCODE%
