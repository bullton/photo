'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const file = path.join(__dirname, '..', 'ai-core.js');
const code = fs.readFileSync(file, 'utf8');

assert.ok(
  !code.includes('\ufffd'),
  'ai-core.js 含有替换字符（编码损坏），请用 UTF-8 无 BOM 保存'
);
new vm.Script(code, { filename: file });

console.log('ai-core parse ok');
