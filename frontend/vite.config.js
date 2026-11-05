import {defineConfig} from 'vite';
export default defineConfig({base:'/chat/',build:{outDir:'../src/agent_base/static/chat',emptyOutDir:true},server:{proxy:{'/api':'http://127.0.0.1:8765'}}});
