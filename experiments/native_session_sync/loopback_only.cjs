// Loaded only by the synthetic WorkBuddy probe.
const net = require('node:net');
const original = net.Socket.prototype.connect;
const local = (host) => !host || host === 'localhost' || host === '::1' ||
  /^127\.\d+\.\d+\.\d+$/.test(host);

net.Socket.prototype.connect = function (...args) {
  const first = Array.isArray(args[0]) ? args[0][0] : args[0];
  const host = typeof first === 'number' ? args[1] :
    first && typeof first === 'object' && !first.path ? first.host : undefined;
  if (typeof host === 'string' && !local(host)) {
    const error = Object.assign(new Error('AWB fixture only permits local sockets'), {code: 'EACCES'});
    process.nextTick(() => this.destroy(error));
    return this;
  }
  return original.apply(this, args);
};
