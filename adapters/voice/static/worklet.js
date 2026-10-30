// Mic capture: resample the context rate to 16 kHz PCM16 mono, post 40 ms chunks.
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / 16000; this.t = 0; this.prev = 0;
    this.buf = new Int16Array(640); this.n = 0;
  }
  push(v) {
    v = v < -1 ? -1 : v > 1 ? 1 : v;
    this.buf[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    if (this.n === this.buf.length) { this.port.postMessage(this.buf.buffer.slice(0)); this.n = 0; }
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      for (let k = 0; k < ch.length; k++) {
        const s = ch[k];
        while (this.t <= 1) { this.push(this.prev + (s - this.prev) * this.t); this.t += this.step; }
        this.t -= 1; this.prev = s;
      }
    }
    return true;
  }
}
registerProcessor('pcm-capture', PcmCapture);
