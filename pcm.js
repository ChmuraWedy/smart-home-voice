class VoicePCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.reset();
    this.port.onmessage = ({ data }) => {
      if (data === 'flush') {
        this.send();
        this.port.postMessage('flushed');
      }
    };
  }

  reset() {
    this.buffer = new ArrayBuffer(960 * 2);
    this.view = new DataView(this.buffer);
    this.samples = 0;
  }

  send() {
    if (!this.samples) return;
    const chunk = this.buffer.slice(0, this.samples * 2);
    this.port.postMessage(chunk, [chunk]);
    this.reset();
  }

  process(inputs) {
    for (const sample of inputs[0]?.[0] ?? []) {
      const value = Math.max(-1, Math.min(1, sample));
      this.view.setInt16(this.samples * 2, value < 0 ? value * 32768 : value * 32767, true);
      this.samples += 1;
      if (this.samples === 960) this.send();
    }
    return true;
  }
}

registerProcessor('voice-pcm', VoicePCM);
