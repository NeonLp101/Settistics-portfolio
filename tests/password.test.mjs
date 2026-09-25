import test from 'node:test';
import assert from 'node:assert/strict';
import gate, {passwordFrom} from '../netlify/edge-functions/password.js';

const basic = (user, pass) => 'Basic ' + Buffer.from(`${user}:${pass}`).toString('base64');
const request = auth => new Request('https://preview.example/', {headers: auth ? {authorization: auth} : {}});
const context = {next: async () => new Response('site')};
const withPassword = value => { globalThis.Netlify = {env: {get: name => (name === 'SITE_PASSWORD' ? value : undefined)}}; };

test('locked when no password is configured', async () => {
  withPassword(undefined);
  assert.equal((await gate(request(basic('x', 'anything')), context)).status, 503);
});
test('asks for a password without credentials', async () => {
  withPassword('correct horse');
  const res = await gate(request(), context);
  assert.equal(res.status, 401);
  assert.match(res.headers.get('www-authenticate'), /^Basic realm=/);
});
test('rejects a wrong password, including a prefix of the right one', async () => {
  withPassword('correct horse');
  assert.equal((await gate(request(basic('riot', 'wrong')), context)).status, 401);
  assert.equal((await gate(request(basic('riot', 'correct')), context)).status, 401);
});
test('serves the site with the right password and any username, never indexed', async () => {
  withPassword('correct horse');
  const res = await gate(request(basic('whoever', 'correct horse')), context);
  assert.equal(res.status, 200);
  assert.equal(await res.text(), 'site');
  assert.equal(res.headers.get('x-robots-tag'), 'noindex, nofollow');
});
test('only riot.txt is reachable without a password', async () => {
  withPassword('correct horse');
  const at = path => gate(new Request('https://preview.example' + path), context);
  assert.equal((await at('/riot.txt')).status, 200);
  for (const path of ['/', '/riot.txt/', '/riot.txt.bak', '/RIOT.txt', '/data/index.json', '/api/riot/health']) {
    assert.equal((await at(path)).status, 401, path);
  }
});
test('passwords containing colons and non-ASCII characters work', () => {
  assert.equal(passwordFrom(basic('u', 'a:b:ü')), 'a:b:ü');
  assert.equal(passwordFrom('Bearer abc'), null);
  assert.equal(passwordFrom('Basic %%%'), null);
});
