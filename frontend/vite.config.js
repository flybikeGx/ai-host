import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {defineConfig} from 'vite';

// First-party MCP packages live outside `frontend/`, so their bare imports
// (react, react-markdown, …) must resolve against this app's node_modules.
// Package-internal imports use relative paths, so only host libraries are
// aliased; their transitive dependencies still resolve from their own location.
const app = fileURLToPath(new URL('.', import.meta.url));
const modules = path.join(app, 'node_modules');
const alias = Object.fromEntries(['react', 'react-dom', 'react-markdown', 'remark-gfm', 'lucide-react']
  .map(name => [name, path.join(modules, name)]));

export default defineConfig({
  base: '/chat/',
  resolve: {alias},
  build: {outDir: '../src/agent_base/static/chat', emptyOutDir: true},
  server: {proxy: {'/api': 'http://127.0.0.1:8765'}},
});
