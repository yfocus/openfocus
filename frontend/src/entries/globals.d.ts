/* SPDX-License-Identifier: Apache-2.0 */
export {};

declare global {
  type OpenFocusRemoteTerminalApi = {
    injectPromptToTerminal?: (
      text: string,
      options?: { bracketedPaste?: boolean; submit?: boolean; focus?: boolean },
    ) => Promise<boolean>;
  };

  interface Window {
    OpenFocusRemoteTerminal?: {
      mount?: (el: HTMLElement, options: Record<string, unknown>) => OpenFocusRemoteTerminalApi | void;
    };
    toast?: (message: string) => void;
  }

  interface HTMLElement {
    __openfocusRemoteTerminal?: OpenFocusRemoteTerminalApi;
  }
}
