/* SPDX-License-Identifier: Apache-2.0 */

export type InspirationBusyPollingState = {
  hasSpace: boolean;
  waiting: boolean;
  publishing: boolean;
  timer: number;
};

export type InspirationBusyStatus = {
  is_waiting?: boolean;
  is_publishing?: boolean;
};

export type InspirationBusyTimer = {
  setTimeout: (callback: () => void, delay: number) => number;
  clearTimeout: (timer: number) => void;
};

export type InspirationBusyPollingOptions = {
  state: InspirationBusyPollingState;
  spaceId: number;
  fetchStatus: (spaceId: number) => Promise<InspirationBusyStatus>;
  onStillBusy: (status: { waiting: boolean; publishing: boolean }) => void;
  onDone: () => void | Promise<void>;
  timer: InspirationBusyTimer;
  defaultDelay?: number;
  errorDelay?: number;
};

export type InspirationBusyPollingController = {
  schedule: (delay?: number) => void;
  cleanup: () => void;
};

const DEFAULT_BUSY_POLL_DELAY = 900;
const DEFAULT_BUSY_POLL_ERROR_DELAY = 1400;

export function createInspirationBusyPollingController(options: InspirationBusyPollingOptions): InspirationBusyPollingController {
  const defaultDelay = options.defaultDelay ?? DEFAULT_BUSY_POLL_DELAY;
  const errorDelay = options.errorDelay ?? DEFAULT_BUSY_POLL_ERROR_DELAY;
  let generation = 0;

  const clearExistingTimer = () => {
    if (!options.state.timer) return;
    options.timer.clearTimeout(options.state.timer);
    options.state.timer = 0;
  };

  const isCurrent = (pollGeneration: number) => pollGeneration === generation;

  const schedule = (delay = defaultDelay) => {
    clearExistingTimer();
    if (!options.state.hasSpace || !options.spaceId || (!options.state.waiting && !options.state.publishing)) return;

    const pollGeneration = ++generation;
    options.state.timer = options.timer.setTimeout(() => {
      void (async () => {
        try {
          const data = await options.fetchStatus(options.spaceId);
          if (!isCurrent(pollGeneration)) return;

          const waiting = !!data.is_waiting;
          const publishing = !!data.is_publishing;
          if (waiting || publishing) {
            options.state.waiting = waiting;
            options.state.publishing = publishing;
            options.onStillBusy({ waiting, publishing });
            if (!isCurrent(pollGeneration)) return;
            schedule(defaultDelay);
            return;
          }
          await options.onDone();
        } catch (_) {
          if (!isCurrent(pollGeneration)) return;
          schedule(errorDelay);
        }
      })();
    }, Number(delay || defaultDelay));
  };

  return {
    schedule,
    cleanup: () => {
      clearExistingTimer();
      generation += 1;
    },
  };
}
