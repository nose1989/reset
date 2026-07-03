// GGSEL (and some other platforms) reject chat attachments larger than
// 2000x2000px. Downscale oversized images in the browser before upload so the
// send succeeds; images already within the limit are returned untouched.
const MAX_DIM = 2000;

export async function downscaleImage(file: File): Promise<File> {
  if (!file.type.startsWith("image/")) return file;
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error("image load failed"));
      el.src = url;
    });
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    if (!w || !h || (w <= MAX_DIM && h <= MAX_DIM)) return file;
    const scale = Math.min(MAX_DIM / w, MAX_DIM / h);
    const cw = Math.max(1, Math.round(w * scale));
    const ch = Math.max(1, Math.round(h * scale));
    const canvas = document.createElement("canvas");
    canvas.width = cw;
    canvas.height = ch;
    const ctx = canvas.getContext("2d");
    if (!ctx) return file;
    ctx.drawImage(img, 0, 0, cw, ch);
    const outType = file.type === "image/png" ? "image/png" : "image/jpeg";
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, outType, 0.92),
    );
    if (!blob) return file;
    const name =
      outType === "image/jpeg" && !/\.jpe?g$/i.test(file.name)
        ? `${file.name.replace(/\.[^.]+$/, "")}.jpg`
        : file.name;
    return new File([blob], name, { type: outType });
  } catch {
    return file;
  } finally {
    URL.revokeObjectURL(url);
  }
}
