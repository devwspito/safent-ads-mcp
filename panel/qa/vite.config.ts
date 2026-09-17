import config from '../vite.config';
// Deliberately no arbitrary target URL: this QA entry only reaches the loopback harness.
export default {...config,server:{host:'127.0.0.1',port:5212,strictPort:true,proxy:{'/api':'http://127.0.0.1:5211','/qa-info':'http://127.0.0.1:5211','/qa-counts':'http://127.0.0.1:5211'}}};
