#include <M5Core2.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <set>

// ----------------- RFID UART -----------------
#define UHF_RX 13
#define UHF_TX 14
#define UHF_BAUD 115200
HardwareSerial &UHF = Serial2;

// ----------------- WiFi & Server -----------------
const char* WIFI_SSID  = "EBOX-5443";
const char* WIFI_PASS  = "27479099f3";
const char* SERVER_URL = "http://raspberrypi.local:5000/post_step2";
const char* STATION_NAME = "Station2";

// ----------------- UHF Commands -----------------
const uint8_t WAKE_CMD[]  = {0xBB,0x00,0x03,0x00,0x03,0x01,0x95,0x7E};
const uint8_t SET_POWER_MAX[] = {0xBB,0x00,0xB6,0x00,0x02,0x1E,0x1E,0xE6,0x7E};
const uint8_t SINGLE_INVENTORY[] = {0xBB,0x00,0x22,0x00,0x00,0x22,0x7E};

// *** Real STOP command ***
const uint8_t STOP_READING[] = {0xBB,0x00,0xFF,0x00,0x00,0xFF,0x7E};

// ----------------- Scan control -----------------
bool scanningEnabled = true;
unsigned long lastScan = 0;
const unsigned long SCAN_INTERVAL = 5000; 
const unsigned long BURST_TIME = 3000;

// ----------------- Helpers -----------------
String bytesToHex(const uint8_t* p, size_t L) {
  static const char HEXMAP[] = "0123456789ABCDEF";
  String s; s.reserve(L*2);
  for(size_t i=0;i<L;i++){
    uint8_t v=p[i];
    s+=HEXMAP[(v>>4)&0xF];
    s+=HEXMAP[v&0xF];
  }
  return s;
}

void sendUHF(const uint8_t* cmd, size_t len){
  UHF.write(cmd, len);
  delay(20);
}

// ----------------- RFID Read -----------------
bool readTag(String &epcOut) {
  if (!scanningEnabled) return false;

  sendUHF(SINGLE_INVENTORY, sizeof(SINGLE_INVENTORY));
  delay(70);

  uint8_t buf[256]; int n=0;
  while(UHF.available() && n<256) buf[n++] = UHF.read();

  int s=-1,e=-1;
  for(int i=0;i<n;i++) if(buf[i]==0xBB){ s=i; break;}
  for(int j=s+1;j<n;j++) if(buf[j]==0x7E){ e=j; break;}

  if(s<0||e<0) return false;

  size_t L = e-s+1;
  uint8_t* f=&buf[s];

  if((f[1]==0x01||f[1]==0x02) && f[2]==0x22 && L>10){
    epcOut = bytesToHex(f+8, L-10);
    return true;
  }
  return false;
}

// ----------------- HTTP to Pi -----------------
void sendToPi(const String &epc) {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");

  String body = "{\"epc\":\"" + epc + "\",\"station\":\"" + STATION_NAME + "\"}";

  // Force non-chunked JSON
  http.addHeader("Content-Length", String(body.length()));

  int code = http.POST(body);
  Serial.printf("[Station2 POST] %s -> HTTP %d\n", epc.c_str(), code);

  http.end();
}

// ----------------- Burst Scan -----------------
void scanArea(){
  std::set<String> seen;
  unsigned long t0 = millis();

  Serial.println("🔄 3-second scan burst");

  while(millis() - t0 < BURST_TIME){
    if(!scanningEnabled){
      Serial.println("⛔ burst cancelled");
      sendUHF(STOP_READING,sizeof(STOP_READING));
      UHF.flush();
      return;
    }

    String epc;
    if(readTag(epc) && epc.length()>=16 && epc.length()<=32){
      seen.insert(epc);
    }
    delay(40);
  }

  for(auto &epc:seen){
    Serial.printf("📦 %s\n", epc.c_str());
    sendToPi(epc);
  }
}

// ----------------- UI -----------------
void drawScreen(){
  M5.Lcd.fillScreen(TFT_BLACK);
  M5.Lcd.setTextSize(3);
  M5.Lcd.setCursor(40,40);
  M5.Lcd.print(scanningEnabled?"SCANNING":"PAUSED");

  M5.Lcd.fillRect(40,160,100,60,TFT_RED);
  M5.Lcd.setTextSize(2);
  M5.Lcd.setCursor(65,180); M5.Lcd.print("STOP");

  M5.Lcd.fillRect(180,160,100,60,TFT_GREEN);
  M5.Lcd.setCursor(215,180); M5.Lcd.print("GO");
}

// ----------------- WiFi -----------------
void connectWiFi(){
  M5.Lcd.fillScreen(TFT_BLACK);
  M5.Lcd.setCursor(20,100);
  M5.Lcd.setTextSize(2);
  M5.Lcd.print("WiFi...");
  WiFi.begin(WIFI_SSID,WIFI_PASS);
  while(WiFi.status()!=WL_CONNECTED){
    delay(250);
    M5.Lcd.print(".");
  }
  M5.Lcd.println("\nOK");
  delay(300);
}

// ----------------- Setup -----------------
void setup(){
  M5.begin();
  Serial.begin(115200);

  UHF.begin(UHF_BAUD,SERIAL_8N1,UHF_RX,UHF_TX);
  delay(200);

  sendUHF(WAKE_CMD,sizeof(WAKE_CMD));
  sendUHF(SET_POWER_MAX,sizeof(SET_POWER_MAX));

  connectWiFi();
  drawScreen();
}

// ----------------- Loop -----------------
void loop(){
  M5.update();

  if(M5.Touch.ispressed()){
    auto p=M5.Touch.getPressPoint();

    // STOP
    if(p.y>160 && p.y<220 && p.x>40 && p.x<140){
      scanningEnabled=false;
      sendUHF(STOP_READING,sizeof(STOP_READING));
      UHF.flush();
      lastScan = millis();  // prevents instant restart
      drawScreen();
      Serial.println("⛔ STOP pressed");
      delay(300);
    }

    // GO
    if(p.y>160 && p.y<220 && p.x>180 && p.x<280){
      scanningEnabled=true;
      lastScan = millis();
      drawScreen();
      Serial.println("▶️ GO pressed");
      delay(300);
    }
  }

  if(scanningEnabled && millis() - lastScan > SCAN_INTERVAL){
    lastScan = millis();
    scanArea();
  }
}
