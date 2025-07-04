import streamlit as st
import requests
import csv
import io
import os
import joblib
import plotly.express as px
import pandas as pd
from dotenv import load_dotenv
import alert_manager
from db_manager import init_db, save_report 

st.set_page_config(page_title="AI-Powered Real-Time Threat Intelligence Dashboard", layout="wide")
load_dotenv()
init_db()

@st.cache_resource
def load_model():
    return joblib.load("rf_threat_model.pkl")

rf_model = load_model()
st.sidebar.success("✅ AI Model Loaded")

if "fetch_triggered" not in st.session_state:
    st.session_state.fetch_triggered = False

def trigger_fetch():
    st.session_state.fetch_triggered = True

st.title("🔡 AI-Powered Real-Time Threat Intelligence Dashboard")

api_key_env_map = {
    "VirusTotal": "VT_API_KEY",
    "AbuseIPDB": "ABUSEIPDB_API_KEY",
    "AlienVault OTX": "OTX_API_KEY"
}

with st.sidebar.form("input_form"):
    st.header("🔧 Configuration")
    api_choice = st.selectbox("Select Threat Intelligence API", [
        "Hybrid Fallback", "VirusTotal", "AbuseIPDB", "AlienVault OTX"
    ])
    user_api_key = st.text_input("🔐 API Key (leave blank to use .env)", type="password")

    st.header("🔍 Input IP Addresses")
    ip_input = st.text_area("Enter IPs (one per line)")
    uploaded_file = st.file_uploader("Or upload .txt/.csv", type=["txt", "csv"])
    limit = st.slider("Max IPs to analyze", 1, 50, 10)

    fetch_btn = st.form_submit_button("🚀 Fetch Threat Reports", on_click=trigger_fetch)

ip_list = []
if ip_input:
    ip_list = [ip.strip() for ip in ip_input.splitlines() if ip.strip()]
elif uploaded_file:
    content = uploaded_file.read().decode("utf-8").splitlines()
    ip_list = [line.strip() for line in content if line.strip()]
ip_list = ip_list[:limit]

def get_virustotal(ip, key):
    headers = {"x-apikey": key}
    url = f"https://www.virustotal.com/api/v3/ip_addresses/{ip}"
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = resp.json().get("data", {}).get("attributes", {})
        return {
            "IP": ip,
            "Country": data.get("country", "N/A"),
            "ASN": data.get("asn", "N/A"),
            "Malicious": data.get("last_analysis_stats", {}).get("malicious", 0),
            "Suspicious": data.get("last_analysis_stats", {}).get("suspicious", 0),
            "Abuse Confidence": 0,
            "Reputation": 0,
            "Source": "VirusTotal"
        }

def get_abuseipdb(ip, key):
    headers = {"Key": key, "Accept": "application/json"}
    params = {"ipAddress": ip, "maxAgeInDays": "90"}
    url = "https://api.abuseipdb.com/api/v2/check"
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code == 200:
        data = resp.json()["data"]
        return {
            "IP": ip,
            "Country": data.get("countryCode", "N/A"),
            "ISP": data.get("isp", "N/A"),
            "Malicious": 0,
            "Suspicious": 0,
            "Abuse Confidence": data.get("abuseConfidenceScore", 0),
            "Reputation": 0,
            "Source": "AbuseIPDB"
        }

def get_otx(ip, key):
    headers = {"X-OTX-API-KEY": key}
    url = f"https://otx.alienvault.com/api/v1/indicators/IPv4/{ip}/general"
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        return {
            "IP": ip,
            "Country": data.get("country_name", "N/A"),
            "Malicious": 0,
            "Suspicious": 0,
            "Abuse Confidence": 0,
            "Reputation": data.get("reputation", 0),
            "Source": "AlienVault OTX"
        }

api_function_map = {
    "VirusTotal": get_virustotal,
    "AbuseIPDB": get_abuseipdb,
    "AlienVault OTX": get_otx
}

def get_hybrid_report(ip, manual_key=None):
    fallback_keys = {
        "VirusTotal": os.getenv("VT_API_KEY"),
        "AbuseIPDB": os.getenv("ABUSEIPDB_API_KEY"),
        "AlienVault OTX": os.getenv("OTX_API_KEY")
    }
    functions = {
        "VirusTotal": get_virustotal,
        "AbuseIPDB": get_abuseipdb,
        "AlienVault OTX": get_otx
    }

    for name, func in functions.items():
        key_to_use = manual_key if manual_key else fallback_keys.get(name)
        if key_to_use:
            try:
                result = func(ip, key_to_use)
                if result:
                    result["Source"] = name
                    return result
            except:
                continue
    return None

def run_analysis(ip_list):
    results = []
    for ip in ip_list:
        try:
            if api_choice == "Hybrid Fallback":
                report = get_hybrid_report(ip, manual_key=user_api_key)
            else:
                key = user_api_key if user_api_key else os.getenv(api_key_env_map.get(api_choice, ""))
                report = api_function_map[api_choice](ip, key)

            if not report:
                continue

            features_df = pd.DataFrame([{
                "Malicious": report.get("Malicious", 0) or 0,
                "Suspicious": report.get("Suspicious", 0) or 0,
                "Abuse Confidence": report.get("Abuse Confidence", 0) or 0,
                "Reputation": report.get("Reputation", 0) or 0
            }])

            try:
                risk = rf_model.predict(features_df)[0]
                report["AI Risk"] = risk
            except:
                report["AI Risk"] = "Error"

            try:
                alert_manager.check_alerts(report)
                if report.get("Abuse Confidence", 0) > 0 or report.get("Malicious", 0) > 0:
                    st.success(f"📧 Email alert sent for suspicious IP: {report['IP']}")
                    alert_manager.send_email_alert(report['IP'], report)
                    print(f"[✔] Email alert sent for {report['IP']}")
            except Exception as e:
                st.error(f"❌ Failed to send email alert for {report['IP']}")
                print(f"Email alert failed for {report['IP']}: {e}")

            save_report(report)  # Save to database
            results.append(report)
        except Exception as e:
            print(f"Error processing {ip}: {e}")
            continue
    return results

def render_visualizations(results):
    df = pd.DataFrame(results)

    if "Country" in df.columns:
        country_counts = df["Country"].value_counts().reset_index()
        country_counts.columns = ["Country", "Count"]
        bar_fig = px.bar(country_counts, x="Country", y="Count", title="🌍 Top Threat Source Countries",
                         color="Count", color_continuous_scale="reds")
        st.plotly_chart(bar_fig, use_container_width=True)

    if "Malicious" in df.columns and "Suspicious" in df.columns:
        threat_summary = pd.DataFrame({
            "Threat Type": ["Malicious", "Suspicious"],
            "Count": [df["Malicious"].sum(), df["Suspicious"].sum()]
        })
        line_fig = px.line(threat_summary, x="Threat Type", y="Count", markers=True,
                           title="📈 Threat Detection Summary")
        st.plotly_chart(line_fig, use_container_width=True)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=results[0].keys())
    writer.writeheader()
    writer.writerows(results)
    st.download_button("⬇️ Download CSV", output.getvalue(), "threat_reports.csv", "text/csv")

    # Historical Report Viewer
    from db_manager import get_all_reports
    with st.expander("📂 View Stored Reports"):
        stored = get_all_reports()
        if stored:
            df_hist = pd.DataFrame(stored, columns=["ID", "IP", "Abuse", "Malicious", "AI Risk", "Source", "Timestamp"])
            st.dataframe(df_hist)
        else:
            st.info("No historical data found.")

if st.session_state.fetch_triggered:
    st.subheader("📊 Threat Reports")
    results = run_analysis(ip_list)
    for report in results:
        st.markdown(f"### 🔎 {report['IP']}")
        for k, v in report.items():
            if k != "IP":
                st.markdown(f"- **{k}**: `{v}`")
    if results:
        render_visualizations(results)
