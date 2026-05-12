/* SPDX-License-Identifier: Apache-2.0 */

export type FileEntry = {
  name: string;
  rel_path: string;
  kind: string;
  size?: number;
  mtime?: number;
};

export type InspirationBusyState = {
  is_waiting?: boolean;
  is_publishing?: boolean;
};

export type IdPayload = {
  item?: { id?: number };
};
