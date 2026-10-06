# Task Tracker API — GitOps CI/CD with Canary Rollout (Scenario 2)

## Neden bu servis var

Bu, portföydeki eski (3 yıllık, kaynakları artık ayakta olmayan) bootcamp
projelerinden biri değil — özellikle bu senaryo için yazılmış, küçük ama
**gerçekten çalışan** bir servis: kendi SQLite veritabanı, gerçek CRUD
mantığı, 7 testi ve gerçek Prometheus metrikleri (`/metrics`) var. Amaç,
CI/CD + canary + rollback mimarisini "sahte bir deploy tiyatrosu" değil,
gerçek trafik/gerçek metrik üzerinden göstermek.

## Mevcut durum

- [X] GKE cluster (`devops-portfolio`, `us-central1-a`)
- [X] Node pool `e2-micro` → `e2-small`'a büyütüldü (bkz. "Yol boyunca
  çıkan gerçek bulgular")
- [X] ArgoCD kurulu
- [X] Argo Rollouts controller kurulu
- [X] Prometheus kurulu (kube-prometheus-stack, Grafana kapalı — bkz. aşağı)
- [ ] Bu servisin ilk deploy'u — **Adım 3**
- [ ] ServiceMonitor + gerçek bir canary rollout — **Adım 4-5**
- [ ] Kasıtlı kötü sürümle rollback kanıtı — **Adım 6**

---

## Yol boyunca çıkan gerçek bulgular (bu da senaryonun bir parçası)

Bu bölümü bilinçli olarak sildirmedik — bir portföyde "her şey ilk seferde
sorunsuz çalıştı" demek, gerçek saha deneyiminden çok bir tutorial'a
benziyor. Aşağıdakiler gerçekten karşılaştığımız ve çözdüğümüz sorunlar:

**1. Argo Rollouts CRD'leri `kubectl apply` ile kurulamadı**
`analysistemplates.argoproj.io` CRD'si çok büyük olduğu için
`last-applied-configuration` annotation'ı 262144 byte sınırını aştı.
Çözüm: `kubectl apply --server-side` kullanmak (annotation'a sığdırmaya
çalışmadan doğrudan API server üzerinden uygular).

**2. `e2-micro` node'da image pull'u ~8 dakika sürdü**
İlk Argo Rollouts controller image'ı (`quay.io/argoproj/argo-rollouts`)
node'a inmesi normalden çok uzun sürdü — `e2-micro`'nun paylaşımlı
(burstable) CPU'su decompress/extract işlemini ciddi şekilde yavaşlattı.
Hata değildi, sadece bu makine tipinin gerçek bir sınırlamasıydı.

**3. `e2-micro`'nun allocatable memory'si, uygulama hiç deploy edilmeden
%99 doluydu**
Node sadece 622Mi allocatable memory'ye sahipti ve bunun neredeyse tamamı
GKE'nin **zorunlu** sistem bileşenleri (`kube-dns`, `gke-metrics-agent`,
`kube-state-metrics`, CSI driver, vs.) tarafından önceden request
edilmişti. Cloud Logging addon'unu (`--logging=NONE`) kapatarak yer açmayı
denedik; GKE'nin reconciler'ı boşalan yeri hemen başka bir yönetilen
bileşenle doldurdu — toplam neredeyse değişmedi. Bu, addon'ları tek tek
kapatarak çözülecek bir durum değildi: **tek bir `e2-micro` node'da GKE'nin
kendi taban yükü, node kapasitesinin büyük kısmını yapısal olarak
kaplıyordu.**

**4. Çözüm: `e2-micro` → `e2-small`**
GCP hesabının "Always Free" değil, **90 günlük/€264'lük bir deneme (Free
Trial) kredisi** olduğunu fark ettik (Billing → Overview'da görünüyor).
Bu, tek bir `e2-micro`'ya sıkışıp BestEffort/no-resource-request gibi
kırılgan workaround'larla uğraşmaktansa, node pool'u gerçekçi bir boyuta
(`e2-small`, 2 vCPU/2GB) büyütmeyi bu kredi kapsamında anlamlı kıldı:

```bash
gcloud container node-pools create larger-pool \
  --cluster devops-portfolio --zone us-central1-a \
  --machine-type e2-small --num-nodes 1

kubectl cordon <eski-node-adi>
kubectl drain <eski-node-adi> --ignore-daemonsets --delete-emptydir-data

gcloud container node-pools delete default-pool \
  --cluster devops-portfolio --zone us-central1-a
```

Sonrasında memory kullanımı %99'dan %54'e düştü — `task-tracker-api`
pod'larına normal `resources.requests/limits` ile (bkz. `k8s/rollout.yaml`)
rahatça yer var.

**5. Google Managed Service for Prometheus (GMP) bileşenleri aylardır
`Pending`**
Cluster'da `gmp-system` namespace'inde GKE'nin varsayılan kurduğu bir GMP
collector zaten vardı, ama `gmp-operator`/`rule-evaluator`/`alertmanager`
pod'ları (rule/alerting kısmı) kaynak yetersizliğinden 4+ saattir hiç
schedule olamamıştı — bizim bu oturumda bozduğumuz bir şey değildi. Bunu
düzeltmeye uğraşmak yerine, kendi `kube-prometheus-stack`'imizi kurmayı
tercih ettik (artık `e2-small`'da rahatça sığıyor); GMP'nin rule/alerting
kısmıyla hiç uğraşmadık çünkü ihtiyacımız yoktu.

**6. Grafana `CrashLoopBackOff`**
`kube-prometheus-stack` içindeki Grafana sürekli çöktü. AnalysisTemplate'imiz
zaten Prometheus'u doğrudan PromQL ile sorguluyor — Grafana sadece görsel
dashboard için, bu senaryoda zorunlu değil. Debug etmek yerine kapattık:

```bash
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --reuse-values --set grafana.enabled=false
```

---

## Adım 1 — Argo Rollouts controller'ı kur ✅ (tamamlandı)

```bash
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts --server-side -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
kubectl get pods -n argo-rollouts   # Running olmalı
```

Kubectl plugin'i de kur (rollout durumunu izlemek için):

```bash
curl -LO https://github.com/argoproj/argo-rollouts/releases/latest/download/kubectl-argo-rollouts-linux-amd64
chmod +x kubectl-argo-rollouts-linux-amd64
sudo mv kubectl-argo-rollouts-linux-amd64 /usr/local/bin/kubectl-argo-rollouts
```

## Adım 2 — Prometheus kur (Helm) ✅ (tamamlandı)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
kubectl create namespace monitoring
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --set grafana.enabled=false \
  --set prometheus.prometheusSpec.resources.requests.memory=256Mi \
  --set prometheus.prometheusSpec.resources.limits.memory=512Mi
```

Doğrulama:

```bash
kubectl get pods -n monitoring
```

## Adım 3 — image'i gerçek registry'e (ghcr.io) bağla ⬅ **sıradaki adım**

1. GitHub'da bu kod için yeni bir repo oluştur: `ser-2007/task-tracker-api`
2. Bu klasörü o repoya push et
3. Repo → Settings → Actions → General → Workflow permissions → **"Read and
   write permissions"** seçili olsun (CI'nin ghcr.io'ya push edebilmesi ve
   manifest commit'i atabilmesi için)
4. Bu repodaki `k8s/rollout.yaml` içindeki image adını kendi GitHub
   kullanıcı adınla eşleştiğinden emin ol (`ghcr.io/ser-2007/...` zaten
   doğru, farklıysa güncelle)
5. `main` branch'e push et → Actions sekmesinden pipeline'ı izle:
   test → build → Trivy scan → ghcr.io'ya push → manifest'te image tag bump

## Adım 4 — ArgoCD Application'ı oluştur

```bash
kubectl apply -f argocd/application.yaml
argocd app get task-tracker-api
argocd app sync task-tracker-api   # automated sync açık, normalde otomatik senkronize olur
```

## Adım 5 — ServiceMonitor'ü uygula ve job label'ını doğrula

```bash
kubectl apply -f k8s/servicemonitor.yaml
```

Prometheus UI'da (`kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090`)
Status → Targets'tan `task-tracker-api` hedefinin `UP` olduğunu ve
gerçek `job` label değerini kontrol et — `k8s/analysis-template.yaml`
içindeki `job="task-tracker-api"` değeriyle eşleşmiyorsa orada düzelt.

## Adım 6 — gerçek bir canary rollout tetikle

```bash
curl http://<task-tracker-api-service-ip>/tasks -X POST -d '{"title":"demo"}' -H "Content-Type: application/json"
kubectl argo rollouts get rollout task-tracker-api --watch
```

Küçük bir kod değişikliği yapıp (ör. yeni bir endpoint ekle) push et,
rollout'un 25% → analiz → 50% → 100% adımlarını gerçek metriklerle
geçtiğini izle ve çıktıyı bu README'ye ekle.

## Adım 7 — kasıtlı kötü bir sürüm ile rollback'i kanıtla

`app.py`'de `/health` endpoint'ine yapay bir gecikme veya hata oranı ekleyip
push et; AnalysisTemplate'in bunu yakalayıp rollout'u durdurduğunu
(`RolloutAborted`/`Degraded`) gerçek `kubectl argo rollouts get rollout`
çıktısıyla belgele. Sonra değişikliği geri al.

---

## Notlar / bilinçli sınırlamalar

- **SQLite + emptyDir**: Veriler pod yeniden başladığında / canary pod'ları
  arasında paylaşılmaz.
- **Basic canary (trafficRouting yok)**: Cluster'da Istio/NGINX Ingress gibi
  bir trafik yönlendirme katmanı yok, bu yüzden Argo Rollouts "basic canary"
  modunda (replica oranına dayalı ağırlıklandırma) çalışıyor —
