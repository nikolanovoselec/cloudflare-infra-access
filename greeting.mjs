// Serialized as the Node entrypoint. Uses only built-ins and no caller inputs.
export function readGreeting(host, deadline) {
  const net = require('node:net');
  const http = require('node:http');
  const budget = Math.min(deadline - Date.now(), 30_000);
  if (budget <= 0) return;
  setTimeout(() => process.exit(0), budget);
  let result;
  const server = http.createServer((req, res) => {
    if (req.method !== 'GET' || req.url !== '/result') { res.writeHead(404).end(); return; }
    if (!result) { res.writeHead(425).end(); return; }
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(result));
  });
  server.on('error', () => process.exit(1));
  server.listen(8080, '0.0.0.0', () => {
    if (Date.now() >= deadline) return;
    let tcpConnected = false;
    let prefix = Buffer.alloc(0);
    const socket = net.connect({ host, port: 22, family: 4 });
    const timeout = setTimeout(() => finish('tcp-timeout'), 5_000);
    function finish(stage) {
      if (result) return;
      clearTimeout(timeout);
      socket.destroy();
      result = { stage, tcpConnected, sshGreeting: stage === 'greeting' };
    }
    socket.once('connect', () => { tcpConnected = true; });
    socket.once('error', () => finish('tcp-error'));
    socket.once('close', () => finish('tcp-closed'));
    socket.on('data', data => {
      // Only inspect up to 255 bytes of the first line. Never write to the SSH socket.
      prefix = Buffer.concat([prefix, data.subarray(0, 255 - prefix.length)]);
      const newline = prefix.indexOf(10);
      if (newline >= 0) {
        const line = prefix.subarray(0, newline + 1).toString('latin1');
        finish(/^SSH-(?:2\.0|1\.99)-[!-~]+(?: [ -~]*)?\r?\n$/.test(line) ? 'greeting' : 'non-ssh');
      } else if (prefix.length === 255) finish('non-ssh');
    });
  });
}
