// Same size/quality and perspective-warning rules as the mainline browser flow.
export function readFileDataUrl(file:File){return new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=()=>reject(new Error('无法读取本机文件'));reader.readAsDataURL(file)})}
export function loadPrivacyImage(dataUrl:string){return new Promise<HTMLImageElement>((resolve,reject)=>{const image=new Image();image.onload=()=>resolve(image);image.onerror=()=>reject(new Error('无法读取图片，已阻止发送'));image.src=dataUrl})}
export function compressImage(file:File,sourceDataUrl:string){
 return new Promise<string>((resolve,reject)=>{if(file.size>25*1024*1024){reject(new Error(`图片“${file.name}”超过25MiB，请先压缩后再上传`));return}resolve(sourceDataUrl)}).then(dataUrl=>{
  if(!String(file.type).startsWith('image/'))return dataUrl
  return new Promise<string>((resolve,reject)=>{const image=new Image();image.onload=()=>{if(Math.min(image.naturalWidth,image.naturalHeight)<700){reject(new Error(`图片“${file.name}”短边低于700px，请使用手机原图而不是聊天软件缩略图`));return}if(image.naturalWidth*image.naturalHeight>40_000_000){reject(new Error(`图片“${file.name}”解码后超过4000万像素，请先缩小`));return}const longest=Math.max(image.naturalWidth,image.naturalHeight),scale=Math.min(1,3000/longest),canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(image.naturalWidth*scale));canvas.height=Math.max(1,Math.round(image.naturalHeight*scale));canvas.getContext('2d')!.drawImage(image,0,0,canvas.width,canvas.height);resolve(canvas.toDataURL('image/jpeg',0.86))};image.onerror=()=>reject(new Error('图片无法解码'));image.src=dataUrl})
 })
}
export async function detectPerspectiveAnomaly(dataUrl:string){
 try{
  const image=await loadPrivacyImage(dataUrl),width=180,height=Math.max(180,Math.round(width*image.naturalHeight/image.naturalWidth)),canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;const context=canvas.getContext('2d',{willReadFrequently:true})!;context.drawImage(image,0,0,width,height);const pixels=context.getImageData(0,0,width,height).data
  const scanBand=(fraction:number)=>{const center=Math.round(height*fraction),radius=Math.max(3,Math.round(height*0.025)),brightColumns=new Array<number>(width).fill(0);let samples=0;for(let y=Math.max(0,center-radius);y<=Math.min(height-1,center+radius);y++){samples++;for(let x=0;x<width;x++){const offset=(y*width+x)*4,luminance=0.2126*pixels[offset]!+0.7152*pixels[offset+1]!+0.0722*pixels[offset+2]!;if(luminance>=205)brightColumns[x]!++}}const threshold=Math.ceil(samples*0.58),valid=brightColumns.map(count=>count>=threshold);let best:{start:number;end:number}|null=null,start=-1;for(let x=0;x<=width;x++){if(x<width&&valid[x]){if(start<0)start=x;continue}if(start>=0){const end=x-1;if(end-start+1>=width*0.45&&(!best||end-start>best.end-best.start))best={start,end};start=-1}}return best}
  const upper=scanBand(0.20),lower=scanBand(0.80);if(!upper||!lower)return false;const leftShift=Math.abs(upper.start-lower.start)/width,rightShift=Math.abs(upper.end-lower.end)/width;return Math.max(leftShift,rightShift)>=0.075
 }catch{return false}
}
