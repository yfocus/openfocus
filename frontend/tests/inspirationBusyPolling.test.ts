/* SPDX-License-Identifier: Apache-2.0 */
import {
  createInspirationBusyPollingController,
  type InspirationBusyPollingState,
  type InspirationBusyStatus,
} from '../src/lib/inspirationBusyPolling.js';

type TestCase = {
  name: string;
  run: () => void | Promise<void>;
};

type ScheduledTimer = {
  id: number;
  delay: number;
  callback: () => void;
  cleared: boolean;
};


type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
};

type FakeTimer = {
  timers: ScheduledTimer[];
  cleared: number[];
  setTimeout: (callback: () => void, delay: number) => number;
  clearTimeout: (timer: number) => void;
  latest: () => ScheduledTimer;
};

function assertEqual<T>(actual: T, expected: T, message?: string): void {
  if (actual !== expected) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${String(expected)}, got ${String(actual)}`);
  }
}

function assertDeepEqual(actual: unknown, expected: unknown, message?: string): void {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  if (actualJson !== expectedJson) {
    throw new Error(`${message ? `${message}: ` : ''}expected ${expectedJson}, got ${actualJson}`);
  }
}


function createDeferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  let reject: (error: unknown) => void = () => undefined;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

async function flushPending(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

function createFakeTimer(): FakeTimer {
  const fake: FakeTimer = {
    timers: [],
    cleared: [],
    setTimeout(callback: () => void, delay: number): number {
      const id = this.timers.length + 1;
      this.timers.push({ id, delay, callback, cleared: false });
      return id;
    },
    clearTimeout(timer: number): void {
      this.cleared.push(timer);
      const found = this.timers.find((item) => item.id === timer);
      if (found) found.cleared = true;
    },
    latest(): ScheduledTimer {
      const latest = this.timers[this.timers.length - 1];
      if (!latest) throw new Error('expected scheduled timer');
      return latest;
    },
  };
  return fake;
}

function createHarness(options: {
  state?: Partial<InspirationBusyPollingState>;
  statuses?: InspirationBusyStatus[];
  failFetch?: boolean;
} = {}) {
  const state: InspirationBusyPollingState = {
    hasSpace: true,
    waiting: true,
    publishing: false,
    timer: 0,
    ...options.state,
  };
  const timer = createFakeTimer();
  const stillBusy: Array<{ waiting: boolean; publishing: boolean }> = [];
  let doneCalls = 0;
  let fetchCalls = 0;
  const statuses = [...(options.statuses || [])];
  const controller = createInspirationBusyPollingController({
    state,
    spaceId: 42,
    fetchStatus: async (spaceId) => {
      fetchCalls += 1;
      assertEqual(spaceId, 42, 'space id');
      if (options.failFetch) throw new Error('fetch failed');
      return statuses.shift() || { is_waiting: false, is_publishing: false };
    },
    onStillBusy: (status) => stillBusy.push(status),
    onDone: () => { doneCalls += 1; },
    timer,
  });
  return {
    state,
    timer,
    stillBusy,
    get doneCalls() { return doneCalls; },
    get fetchCalls() { return fetchCalls; },
    controller,
  };
}

async function fire(timer: ScheduledTimer): Promise<void> {
  timer.callback();
  await Promise.resolve();
  await Promise.resolve();
}

const tests: TestCase[] = [
  {
    name: 'does not schedule when there is no space',
    run: () => {
      const harness = createHarness({ state: { hasSpace: false, timer: 7 } });
      harness.controller.schedule();
      assertDeepEqual(harness.timer.cleared, [7]);
      assertEqual(harness.state.timer, 0);
      assertEqual(harness.timer.timers.length, 0);
    },
  },
  {
    name: 'does not schedule when state is not busy',
    run: () => {
      const harness = createHarness({ state: { waiting: false, publishing: false } });
      harness.controller.schedule();
      assertEqual(harness.timer.timers.length, 0);
    },
  },
  {
    name: 'clears previous timer before scheduling',
    run: () => {
      const harness = createHarness({ state: { timer: 10 } });
      harness.controller.schedule(400);
      assertDeepEqual(harness.timer.cleared, [10]);
      assertEqual(harness.timer.timers.length, 1);
      assertEqual(harness.timer.latest().delay, 400);
      assertEqual(harness.state.timer, 1);
    },
  },
  {
    name: 'still busy updates state and reschedules with 900',
    run: async () => {
      const harness = createHarness({ statuses: [{ is_waiting: false, is_publishing: true }] });
      harness.controller.schedule(400);
      await fire(harness.timer.latest());
      assertEqual(harness.state.waiting, false);
      assertEqual(harness.state.publishing, true);
      assertDeepEqual(harness.stillBusy, [{ waiting: false, publishing: true }]);
      assertDeepEqual(harness.timer.cleared, [1]);
      assertEqual(harness.timer.latest().delay, 900);
      assertEqual(harness.doneCalls, 0);
    },
  },
  {
    name: 'done calls onDone',
    run: async () => {
      const harness = createHarness({ statuses: [{ is_waiting: false, is_publishing: false }] });
      harness.controller.schedule();
      await fire(harness.timer.latest());
      assertEqual(harness.doneCalls, 1);
      assertEqual(harness.stillBusy.length, 0);
    },
  },
  {
    name: 'error reschedules with 1400',
    run: async () => {
      const harness = createHarness({ failFetch: true });
      harness.controller.schedule();
      await fire(harness.timer.latest());
      assertDeepEqual(harness.timer.cleared, [1]);
      assertEqual(harness.timer.latest().delay, 1400);
      assertEqual(harness.doneCalls, 0);
    },
  },

  {
    name: 'cleanup cancels pending busy poll without callbacks or reschedule',
    run: async () => {
      const state: InspirationBusyPollingState = { hasSpace: true, waiting: true, publishing: false, timer: 0 };
      const timer = createFakeTimer();
      const deferred = createDeferred<InspirationBusyStatus>();
      const stillBusy: Array<{ waiting: boolean; publishing: boolean }> = [];
      let doneCalls = 0;
      let fetchCalls = 0;
      const controller = createInspirationBusyPollingController({
        state,
        spaceId: 42,
        fetchStatus: () => {
          fetchCalls += 1;
          return deferred.promise;
        },
        onStillBusy: (status) => stillBusy.push(status),
        onDone: () => { doneCalls += 1; },
        timer,
      });

      controller.schedule();
      timer.latest().callback();
      assertEqual(fetchCalls, 1);
      controller.cleanup();
      deferred.resolve({ is_waiting: true, is_publishing: false });
      await flushPending();

      assertDeepEqual(timer.cleared, [1]);
      assertEqual(state.timer, 0);
      assertDeepEqual(stillBusy, []);
      assertEqual(doneCalls, 0);
      assertEqual(timer.timers.length, 1);
    },
  },
  {
    name: 'cleanup cancels pending done poll without onDone or reschedule',
    run: async () => {
      const state: InspirationBusyPollingState = { hasSpace: true, waiting: true, publishing: false, timer: 0 };
      const timer = createFakeTimer();
      const deferred = createDeferred<InspirationBusyStatus>();
      const stillBusy: Array<{ waiting: boolean; publishing: boolean }> = [];
      let doneCalls = 0;
      const controller = createInspirationBusyPollingController({
        state,
        spaceId: 42,
        fetchStatus: () => deferred.promise,
        onStillBusy: (status) => stillBusy.push(status),
        onDone: () => { doneCalls += 1; },
        timer,
      });

      controller.schedule();
      timer.latest().callback();
      controller.cleanup();
      deferred.resolve({ is_waiting: false, is_publishing: false });
      await flushPending();

      assertDeepEqual(timer.cleared, [1]);
      assertEqual(state.timer, 0);
      assertDeepEqual(stillBusy, []);
      assertEqual(doneCalls, 0);
      assertEqual(timer.timers.length, 1);
    },
  },
  {
    name: 'cleanup cancels pending error poll without retry reschedule',
    run: async () => {
      const state: InspirationBusyPollingState = { hasSpace: true, waiting: true, publishing: false, timer: 0 };
      const timer = createFakeTimer();
      const deferred = createDeferred<InspirationBusyStatus>();
      const stillBusy: Array<{ waiting: boolean; publishing: boolean }> = [];
      let doneCalls = 0;
      const controller = createInspirationBusyPollingController({
        state,
        spaceId: 42,
        fetchStatus: () => deferred.promise,
        onStillBusy: (status) => stillBusy.push(status),
        onDone: () => { doneCalls += 1; },
        timer,
      });

      controller.schedule();
      timer.latest().callback();
      controller.cleanup();
      deferred.reject(new Error('fetch failed'));
      await flushPending();

      assertDeepEqual(timer.cleared, [1]);
      assertEqual(state.timer, 0);
      assertDeepEqual(stillBusy, []);
      assertEqual(doneCalls, 0);
      assertEqual(timer.timers.length, 1);
    },
  },
  {
    name: 'cleanup clears timer',
    run: () => {
      const harness = createHarness();
      harness.controller.schedule();
      harness.controller.cleanup();
      assertDeepEqual(harness.timer.cleared, [1]);
      assertEqual(harness.state.timer, 0);
    },
  },
];

let failures = 0;
for (const test of tests) {
  try {
    await test.run();
    console.log(`ok - ${test.name}`);
  } catch (error) {
    failures += 1;
    console.error(`not ok - ${test.name}`);
    console.error(error instanceof Error ? error.message : String(error));
  }
}

if (failures) {
  throw new Error(`${failures} frontend Inspiration busy polling test(s) failed`);
}
