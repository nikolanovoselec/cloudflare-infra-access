import { DurableObject } from 'cloudflare:workers';
import { handleRequest, runProbe } from './probe.mjs';

export class AlphaProbeContainer extends DurableObject {
  async probe() {
    return runProbe(this.ctx, this.env.GATEWAY_IDENTITY, Number(this.env.EXPIRES_AT), {
      ip: this.env.PROBE_IP, identity: this.env.PROBE_IDENTITY,
    });
  }
}

export default { fetch: handleRequest };
