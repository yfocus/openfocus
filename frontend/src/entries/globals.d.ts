export {};

declare global {
  interface Window {
    OpenFocusRemoteTerminal?: {
      mount?: (el: HTMLElement, options: Record<string, unknown>) => void;
    };
    toast?: (message: string) => void;
  }

  interface HTMLElement {
    __openfocusRemoteTerminal?: {
      injectPromptToTerminal?: (
        text: string,
        options?: { bracketedPaste?: boolean; submit?: boolean; focus?: boolean },
      ) => Promise<boolean>;
    };
  }
}
