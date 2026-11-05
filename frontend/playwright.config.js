import {defineConfig} from '@playwright/test';
import fs from 'node:fs';
const chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
export default defineConfig({
  testDir:'./tests',timeout:45000,workers:1,
  use:{baseURL:'http://127.0.0.1:8879',viewport:{width:1440,height:1000},headless:true,
    launchOptions:fs.existsSync(chrome)?{executablePath:chrome}:{}},
  webServer:{command:'../.venv/bin/python ../scripts/serve_chat_fixture.py',url:'http://127.0.0.1:8879/chat',reuseExistingServer:false,timeout:60000},
});
