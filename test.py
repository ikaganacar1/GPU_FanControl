import pynvml                                                                                
pynvml.nvmlInit()                                                                            
h = pynvml.nvmlDeviceGetHandleByIndex(0)                                                     
pynvml.nvmlDeviceSetFanSpeed_v2(h, 0, 60)                                                    
print('Fan set to 60% - SUCCESS')                                                            
import time; time.sleep(2)                                                                   
pynvml.nvmlDeviceSetDefaultFanSpeed_v2(h, 0)                                                 
print('Reset to auto')                                                                       
pynvml.nvmlShutdown()                                                                        
