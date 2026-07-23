import "@testing-library/jest-dom";

// Recharts' ResponsiveContainer observes its container element via
// ResizeObserver, which jsdom does not implement. Provide a minimal stub so
// chart components mount in unit tests without throwing. The stub reports a
// fixed size and invokes the callback synchronously, matching the subset of
// the API surface Recharts relies on.
class ResizeObserverStub {
  constructor(callback) {
    this.callback = callback;
  }

  observe(target) {
    this.callback(
      [
        {
          target,
          contentRect: {
            x: 0,
            y: 0,
            width: 320,
            height: 240,
            top: 0,
            left: 0,
            bottom: 240,
            right: 320,
          },
        },
      ],
      this,
    );
  }

  unobserve() {
    /* no-op */
  }

  disconnect() {
    /* no-op */
  }
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = ResizeObserverStub;
}
