"use strict";

function bdbCrc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function bdbZipWrite16(view, offset, value) {
  view.setUint16(offset, value, true);
}

function bdbZipWrite32(view, offset, value) {
  view.setUint32(offset, value >>> 0, true);
}

function bdbZipDosTimestamp(date) {
  const year = Math.max(1980, date.getFullYear());
  return {
    time: (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2),
    date: ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate()
  };
}

function bdbZipSingleFile(filename, content) {
  const encoder = new TextEncoder();
  const name = encoder.encode(filename);
  const data = encoder.encode(content);
  const crc = bdbCrc32(data);
  const stamp = bdbZipDosTimestamp(new Date());
  const localLength = 30 + name.length + data.length;
  const centralLength = 46 + name.length;
  const result = new Uint8Array(localLength + centralLength + 22);
  const view = new DataView(result.buffer);

  bdbZipWrite32(view, 0, 0x04034b50);
  bdbZipWrite16(view, 4, 20);
  bdbZipWrite16(view, 6, 0x0800);
  bdbZipWrite16(view, 8, 0);
  bdbZipWrite16(view, 10, stamp.time);
  bdbZipWrite16(view, 12, stamp.date);
  bdbZipWrite32(view, 14, crc);
  bdbZipWrite32(view, 18, data.length);
  bdbZipWrite32(view, 22, data.length);
  bdbZipWrite16(view, 26, name.length);
  bdbZipWrite16(view, 28, 0);
  result.set(name, 30);
  result.set(data, 30 + name.length);

  const central = localLength;
  bdbZipWrite32(view, central, 0x02014b50);
  bdbZipWrite16(view, central + 4, 20);
  bdbZipWrite16(view, central + 6, 20);
  bdbZipWrite16(view, central + 8, 0x0800);
  bdbZipWrite16(view, central + 10, 0);
  bdbZipWrite16(view, central + 12, stamp.time);
  bdbZipWrite16(view, central + 14, stamp.date);
  bdbZipWrite32(view, central + 16, crc);
  bdbZipWrite32(view, central + 20, data.length);
  bdbZipWrite32(view, central + 24, data.length);
  bdbZipWrite16(view, central + 28, name.length);
  bdbZipWrite16(view, central + 30, 0);
  bdbZipWrite16(view, central + 32, 0);
  bdbZipWrite16(view, central + 34, 0);
  bdbZipWrite16(view, central + 36, 0);
  bdbZipWrite32(view, central + 38, 0);
  bdbZipWrite32(view, central + 42, 0);
  result.set(name, central + 46);

  const end = central + centralLength;
  bdbZipWrite32(view, end, 0x06054b50);
  bdbZipWrite16(view, end + 4, 0);
  bdbZipWrite16(view, end + 6, 0);
  bdbZipWrite16(view, end + 8, 1);
  bdbZipWrite16(view, end + 10, 1);
  bdbZipWrite32(view, end + 12, centralLength);
  bdbZipWrite32(view, end + 16, localLength);
  bdbZipWrite16(view, end + 20, 0);
  return result;
}

function bdbDownloadDiagnosticsZip(diagnostics) {
  const content = JSON.stringify(diagnostics, null, 2);
  const zip = bdbZipSingleFile("bdb-diagnostics.json", content);
  const blob = new Blob([zip], { type: "application/zip" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `bdb-diagnostics-${new Date().toISOString().replace(/[:.]/g, "-")}.zip`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

globalThis.bdbDownloadDiagnosticsZip = bdbDownloadDiagnosticsZip;
