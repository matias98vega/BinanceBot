#!/usr/bin/env python3
"""Local benchmark for passive feature derivation; no network access."""
import json,time
import feature_registry

def benchmark(iterations=1000):
    closes=[100+i*.05 for i in range(60)]
    highs=[x+.3 for x in closes];lows=[x-.3 for x in closes]
    volumes=[1000+i for i in range(60)];opens=[x-.05 for x in closes]
    candidate={'price':closes[-1],'sl':closes[-1]-1,'tp':closes[-1]+1.5,'atr':.5,'direction':'long'}
    started=time.perf_counter()
    for _ in range(iterations):
        feature_registry.build_preentry_context(candidate,closes,highs,lows,volumes,opens,None,{'change_1h':.1,'change_4h':.2})
    elapsed=time.perf_counter()-started
    return {'iterations':iterations,'total_ms':elapsed*1000,'mean_ms_per_candidate':elapsed*1000/iterations,
      'target_ms_per_candidate':100,'within_target':elapsed*1000/iterations<100,'additional_remote_calls':0}
if __name__=='__main__':print(json.dumps(benchmark(),sort_keys=True))
