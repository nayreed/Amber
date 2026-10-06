"""Numeric answers for the textbook exercises (uses Amber's own functions where possible)."""
import math
from amber import propagation as P, packet_analysis as PA
db=lambda x:10*math.log10(x)
print("== Ch1")
E=0.225*3*3600; print("cell J",E," 20uW years",E/20e-6/3.156e7," load for 15 y uW",E/(15*3.156e7)*1e6," factor",20/(E/(15*3.156e7)*1e6))
print("swaps/day",2e6/(4*365))
print("== Ch3")
print("33dBm W",P.dbm_to_watts(33)," 2uW dBm",P.watts_to_dbm(2e-6)," -97 W",P.dbm_to_watts(-97))
print("sum -70,-73:",P.watts_to_dbm(P.dbm_to_watts(-70)+P.dbm_to_watts(-73))," 4x-73:",P.watts_to_dbm(4*P.dbm_to_watts(-73)))
print("noise 1MHz NF8:",-174+60+8," 100MHz NF6:",-174+80+6)
pdl=30+17-80+1; print("Pdl",pdl,"dBm =",P.dbm_to_watts(pdl)*1e6,"uW; uplink eta .5:",pdl+db(.5)+1-80+17)
print("B for -100 sens, g=3, NF6: MHz",10**((-100-3+174-6)/10)/1e6)
print("== Ch4")
for e in (40,90,150): print("gain 17dBi hp65 err",e,17-min(12*(e/65)**2,30))
print("floor angles: 30->",30*math.sqrt(2.5)," 65->",65*math.sqrt(2.5)," 120->",120*math.sqrt(2.5))
print("sector 60deg err gain (15dBi):",15-12*(60/65)**2, " directivity 90x15:",db(41253/(90*15)))
print("== Ch5")
f=924e6
print("FSPL 50m 924M:",db(1/P.fspl_lin(50,924e6)),"3.5G:",db(1/P.fspl_lin(50,3.5e9)))
print("dBP 30,2,2GHz:",4*(30-1)*(2-1)*2e9/3e8)
print("UMa LOS 300m demo:",float(P.urban_macro_loss_db_38_901(300,f,25,1.5,True)))
print("alpha .01 100m per hop dB:",4.3429*0.01*100)
print("DL/UL change 50->100m LOS:",float(P.urban_macro_loss_db_38_901(100,f))-float(P.urban_macro_loss_db_38_901(50,f)))
print("== Ch6")
lam=3e8/915e6; fs=db(1/P.fspl_lin(4,915e6)); ptag=36+9+2-fs; pbs=ptag+db(.3); pr=pbs+2+9-fs
print("lam",lam,"FSPL4m",fs,"Ptag",ptag,"Pbs",pbs,"Pr",pr)
print("sideband (0.8/pi)^2:",(0.8/math.pi)**2,db((0.8/math.pi)**2))
print("harvest drop eta .7:",db(.3)," .2:",db(.8))
print("== Ch7")
print("P from mean V 2.66:",2.66**2/5000*1e3,"mW, dBm",db(2.66**2/5000*1e3)," mean V^2 var term uW:",0.25**2/5000*1e6)
print("max V:",math.sqrt(1.4e-3*5000)," sum V:",math.sqrt(2.4e-3*5000))
print("min P for 1.7V:",(1.7*105/100)**2/5000*1e6,"uW",db((1.7*1.05)**2/5000*1e3),"dBm")
print("== Ch8")
Rs,Rl=5e3,1e5
def tau(C): return (Rs*Rl/(Rs+Rl))*C
t=tau(200e-6); Vinf=2.486*Rl/(Rl+Rs); print("tau200",t,"t to 1.7",t*math.log(Vinf/(Vinf-1.7)))
print("starved Vs <",1.3*(Rl+Rs)/Rl+300e-6*Rs)
print("dE 200uF",0.5*200e-6*(1.7**2-1.3**2)*1e6," lower Vlow 1.1:",0.5*200e-6*(1.7**2-1.1**2)*1e6)
Vinf1=1.971*Rl/(Rl+Rs); print("node1 t",tau(300e-6)*math.log(Vinf1/(Vinf1-1.7)))
Tc=180e-6/200e-6; Ta=180e-6/(4e-3-200e-6); print("D",200e-6/4e-3,"Tc",Tc,"Ta",Ta,"bursts/s",1/(Tc+Ta))
print("== Ch9")
n,S=20,16; print("opt S=n=20 eff",(1-1/20)**19)
for S in (30,45,60,90):
    ps=(1-1/S)**29; print("S",S,"psucc %.3f eff %.3f E/deliv %.0f uJ"%(ps,30*ps/S,112/ps))
print("Schoute",5+2.39*5,"K=3:",(1-1/10)**19," K=1:",(1-1/10)**59)
print("== Ch10")
N=PA.thermal_noise_watts(100e6,6.0)
def sic(pw,eps=0.0,g=3.0):
    P_=[10**(p/10)*1e-3 for p in pw]; order=sorted(range(len(P_)),key=lambda i:-P_[i]); dec=[];res=[]
    for k,i in enumerate(order):
        interf=sum(P_[j] for j in order[k+1:])+eps*sum(P_[j] for j in order[:k])+N
        s=P_[i]/interf; res.append((i,round(db(s),2)))
        if db(s)>=g: dec.append(i)
        else: break
    return dec,res
print("ex1",sic([-62,-70]))
print("ex2 equal 3:",sic([-70,-70,-70]))
print("ex5 eps=.05",sic([-60,-65,-70],0.05)," eps=.1",sic([-60,-65,-70],0.1)," eps=0",sic([-60,-65,-70],0.0))
print("ratio 4 pkts dB:",3*db(1+10**0.3))
print("== Ch11")
def mf(n1,p,S=10,K=3,rounds=12,target=0.9):
    n=n1; out=[]
    for r in range(rounds):
        a=n*p; ps=(1-p/(S*K))**max(n-1,0); n=n-a*ps; out.append(1-n/n1)
    return out
for p in (1.0,0.5):
    o=mf(50,p); print("p_a",p,"round1 %.3f"%o[0],"rounds to 90%:",next((i+1 for i,v in enumerate(o) if v>=0.9),">12"))
print("unicast round 100 nodes ms",100*15)
print("== Ch12")
print("500 nodes 120s s:",500*120000*13e-6," replications/hour 100n 20s:",3600/(100*20000*13e-6))
print("== Ch14")
import math as m
bear=m.degrees(m.atan2(80,60)); e0=abs((bear-90+180)%360-180); e1=abs((bear-330+180)%360-180)
pl=float(P.urban_macro_loss_db_38_901(100,f,25,1.5,True))
print("bearing",bear,"err s0",e0,"err s120",e1,"gain0",15-12*(e0/65)**2,"gain1",15-min(12*(e1/65)**2,30),"PL",pl,"Pdl",46+15-12*(e0/65)**2-pl)
print("== Ch15")
Q=83.124e-6; print("cycle energy @1.7:",Q*1.7*1e6," @1.3:",Q*1.3*1e6)
print("entry 1.35V burst time ms:",300e-6*0.05/5e-3*1e3," min entry V:",1.3+5e-3*15e-3/300e-6)
print("== Ch18")
print("DER",1-117/130," thr",117/20.001," audible PDR",117/(130-6))
print("== Ch19")
n,S=3.91,8.44; print("eff",n/S*(1-1/S)**(n-1)," S=n:",(1-1/n)**(n-1)," S=2n:",0.5*(1-1/(2*n))**(n-1))
