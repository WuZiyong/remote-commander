import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
const source = readFileSync('worker/index.js', 'utf8');
const tools = readFileSync('worker/tools.json', 'utf8');
mkdirSync('dist/server', {recursive:true});
mkdirSync('dist/.openai', {recursive:true});
writeFileSync('dist/server/index.js', source.replace('import tools from "./tools.json" with { type: "json" };', 'const tools = '+tools+';'));
writeFileSync('dist/.openai/hosting.json', readFileSync('.openai/hosting.json'));
