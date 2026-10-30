@echo off
REM ctxindex 命令行包装器：在工具目录下运行 python -m ctxindex
pushd "%~dp0"
python -m ctxindex %*
popd
