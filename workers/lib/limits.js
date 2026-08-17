// Size & ZIP content limits — port of worker.py check_zip_limits /
// count_zip_so_dex / check_download_size.

import path from "node:path";
import { isZip, listZipEntries } from "./zip.js";

export class Limits {
  /**
   * @param {{isAdmin?: boolean, isPremium?: boolean, filename?: string}} opts
   */
  constructor({ isAdmin = false, isPremium = false, filename = "download" }) {
    this.isAdmin = isAdmin;
    this.isPremium = isPremium;
    this.filename = filename;
  }

  /** Max download size in MB (2000 admin, else 500). */
  maxDownloadMb() {
    return this.isAdmin ? 2000 : 500;
  }

  /** @throws if the file exceeds the download limit */
  checkDownloadSize(totalBytes) {
    const max = this.maxDownloadMb() * 1024 * 1024;
    if (totalBytes && totalBytes > max) {
      throw new Error(
        `File is ${(totalBytes / 1024 / 1024).toFixed(1)} MB — max download limit is ${this.maxDownloadMb()} MB.`
      );
    }
  }

  /**
   * @throws if a ZIP contains more .so/.dex or .apk files than allowed.
   * Free: max 1 .so/.dex + 1 .apk | Premium: max 5 .so/.dex + 2 .apk
   */
  checkZipLimits(zipPath) {
    if (this.isAdmin) return;
    if (path.extname(this.filename).toLowerCase() !== ".zip") return;
    if (!isZip(zipPath)) return;

    const names = listZipEntries(zipPath).map((e) => e.name);
    const soDex = names.filter((n) => /\.(so|dex)$/i.test(n)).length;
    const apks = names.filter((n) => /\.apk$/i.test(n)).length;

    const maxSoDex = this.isPremium ? 5 : 1;
    const maxApk = this.isPremium ? 2 : 1;
    const tier = this.isPremium ? "Premium" : "Free";

    if (soDex > maxSoDex) {
      throw new Error(
        `ZIP contains ${soDex} .so/.dex files — max ${maxSoDex} allowed for ${tier} users.`
      );
    }
    if (apks > maxApk) {
      throw new Error(
        `ZIP contains ${apks} .apk files — max ${maxApk} allowed for ${tier} users.`
      );
    }
  }

  /** Count .so/.dex entries inside a zip (0 if not a zip). */
  countZipSoDex(zipPath) {
    if (path.extname(this.filename).toLowerCase() !== ".zip") return 0;
    if (!isZip(zipPath)) return 0;
    return listZipEntries(zipPath).filter((e) => /\.(so|dex)$/i.test(e.name)).length;
  }
}
