#!/usr/bin/env python3
"""Local PineTS and credential-file diagnostics; never translate/register TV indicators."""
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    from controller.tv_credentials import credentials_present
    configured=credentials_present(ROOT/'service/creds.json')
    print("TradingView凭据："+("文件完整（有效性仍以行情授权为准）" if configured else "等待登录，请启动控制台并按专用Chrome登录引导操作"))
    print("指标来源：PineTS本地；TV指标注册/正文缓存流程已移除")
    code="""const {getDefinition,createCalculator}=require('./service/pinets_runtime');
const version=require('./package-lock.json').packages['node_modules/pinets'].version;
for(const key of ['e9','e20','e7v2','golden','trend','fvg','smart','attack','vwap_pullback']){
const d=getDefinition(key);createCalculator(d,{version});console.log(key+': 本地源码与PineTS依赖可加载 v'+d.version);}"""
    result=subprocess.run(['node','-e',code],cwd=ROOT,check=False)
    return result.returncode or (0 if configured else 1)
if __name__=='__main__':raise SystemExit(main())
