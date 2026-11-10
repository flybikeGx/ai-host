export async function api(path,{token='',method='GET',body,signal}={}) {
  const response=await fetch('/api/'+path,{method,signal,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:body===undefined?undefined:JSON.stringify(body)});
  if(!response.ok){let detail;try{detail=(await response.json()).detail}catch{detail='服务暂时不可用'}const error=new Error(typeof detail==='string'?detail:JSON.stringify(detail));error.status=response.status;throw error;}
  return response.json();
}
