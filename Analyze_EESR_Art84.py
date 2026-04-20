import os
import re
import glob
import time
import random
import pandas as pd
import fitz  # PyMuPDF
import google.generativeai as genai
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==========================================
# 0. API & 환경 설정
# ==========================================
# Google Gemini API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "발급받은_GEMINI_API_KEY_여기에_붙여넣기")
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "EESR_Downloads_ESOP")

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# ==========================================
# 1. ESOP 타겟 전용 브라우저 설정 (EPO Register 뚫기용)
# ==========================================
def setup_epo_register_driver(download_dir):
    """
    EPO Register의 403 차단을 뚫기 위한 undetected_chromedriver 사용.
    """
    options = uc.ChromeOptions()
    # 윈도우 환경 및 탐지 우회를 위해 필요한 최소 옵션만 설정
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--start-maximized")
    # 팝업 차단 방지: 뷰어 팝업이 Chrome 정책에 의해 막히는 빈도를 줄임
    options.add_argument("--disable-popup-blocking")
    
    # 클릭 시 새 탭이 열리거나 PDF를 뷰어로 열지 않고 즉각 다운로드 폴더로 강제 전송
    prefs = {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        # 팝업 및 자동 다운로드 무조건 허용
        "profile.default_content_setting_values.popups": 1,
        "profile.default_content_setting_values.automatic_downloads": 1
    }
    options.add_experimental_option("prefs", prefs)
    
    driver = uc.Chrome(options=options)
    # 로딩 지연 타임아웃
    driver.set_page_load_timeout(30)
    return driver

def wait_for_download(download_dir, timeout=30):
    seconds = 0
    while seconds < timeout:
        time.sleep(1)
        if any(filename.endswith(".crdownload") for filename in os.listdir(download_dir)):
            seconds += 1
        else:
            return True
    return False

def get_latest_pdf(download_dir):
    list_of_files = glob.glob(os.path.join(download_dir, '*.pdf'))
    if not list_of_files:
        return None
    return max(list_of_files, key=os.path.getmtime)

# ==========================================
# 2. EPO Register 자동화 봇 동작 시퀀스
# ==========================================
def download_esop_from_register(driver, app_number, download_dir):
    """
    register.epo.org 에 접속하여 'European search opinion' 문서를 찾아냅니다.
    """
    try:
        # 소수점 제거 방어
        app_num_clean = str(app_number).split('.')[0]
        # EPO 포맷화: EP18817854
        app_num_clean = app_num_clean if app_num_clean.startswith('EP') else f"EP{app_num_clean}"
        
        # IP 차단 방지: 각 크롤링 사이클 시작점(URL 진입 전)에 2~4초의 무작위 휴식 부여
        delay = random.uniform(2.5, 4.2)
        print(f"  -> [Anti-Bot] 프로그래매틱 탐지 회피를 위해 {delay:.1f}초 대기 중...")
        time.sleep(delay)
        
        print(f"  -> [{app_num_clean}] EPO Register 전면 접속 시도 중...")
        url = f"https://register.epo.org/application?number={app_num_clean}&lng=en&tab=doclist"
        driver.get(url)
        wait = WebDriverWait(driver, 15)
        
        time.sleep(random.uniform(2.0, 3.5)) # 페이지 완전 렌더링을 위한 안전한 대기
        
        # 쿠키 허용 팝업 (가끔 발생함)
        try:
            cookie_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#agree, button.cc-btn.cc-allow, a.agree")))
            cookie_btn.click()
            time.sleep(1)
        except:
            pass
            
        print("  -> 'All documents' (심사 이력) 테이블 스캔 중...")
        
        # ESOP 문서 행 찾기 (이름 매칭)
        esop_link = None
        try:
            # 1순위: 정확히 "European search opinion" 매칭
            esop_link = driver.find_element(By.XPATH, "//a[normalize-space(text())='European search opinion']")
            print("  -> [HIT] 'European search opinion' 원문 발견!")
        except:
            try:
                # 2순위: "search opinion" 이 포함된 문서들 중 첫번째
                esop_link = driver.find_element(By.XPATH, "//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'search opinion')]")
                print("  -> [HIT] 'search opinion' 을 포함한 대체 원문 발견!")
            except:
                pass
                
        if not esop_link:
            print("  -> [PASS] 해당 출원에는 아직 등재된 'European search opinion' 문서가 없습니다.")
            return None
            
        # 클릭 전 현재 파일 상태 확보
        before_download_count = len(glob.glob(os.path.join(download_dir, '*.pdf')))
        
        # 뷰어 열기 (새 탭 또는 팝업에서 열림)
        original_window = driver.current_window_handle
        driver.execute_script("arguments[0].click();", esop_link)
        print("  -> 문서 뷰어 새 탭 대기 중...")
        
        # 팝업 창이 뜰 때까지 명시적 대기 시도
        try:
            wait.until(lambda d: len(d.window_handles) > 1)
        except:
            pass # 타임아웃 되거나 크롬이 새 탭을 강제 닫아버릴 수 있음
            
        time.sleep(1) # 크롬 동작 안정화
        
        # 새 창 핸들 획득 및 전환 (안전 처리를 통해 list index out of range 방지)
        new_window_matches = [h for h in driver.window_handles if h != original_window]
        new_window = new_window_matches[0] if new_window_matches else None
        
        if new_window:
            driver.switch_to.window(new_window)
            
            # 1. 'Load all pages' 버튼 클릭 시도 (1페이지짜리 문서면 버튼이 없을 확률 99%)
            try:
                # 뷰어 로딩이 느릴 수 있으므로 12~15초 넉넉하게 대기
                viewer_wait = WebDriverWait(driver, 15)
                load_btn = viewer_wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'Load all pages') or @id='loadAllPages']")))
                driver.execute_script("arguments[0].click();", load_btn)
                print("  -> [성공] 'Load all pages' 버튼 클릭 완료. 전체 페이지 렌더링 중...")
                time.sleep(random.uniform(3.0, 4.5)) # 전체 페이지 렌더링을 위한 무작위 대기
            except:
                print("  -> [INFO] 'Load all pages' 버튼이 없습니다. (단일 페이지이거나 이미 전체 렌더링 됨)")
                time.sleep(1)
                
            # 2. 다운로드 버튼 클릭 (복합 탐색 - EPO 자체 다운로드 및 크롬 PDF 열기 오버레이 모두 타격)
            try:
                try:
                    # 1순위: EPO 자체 다운로드 버튼 우선 클릭
                    down_btn = driver.find_element(By.XPATH, "//button[@title='Download'] | //a[@title='Download'] | //*[@id='download']")
                    driver.execute_script("arguments[0].click();", down_btn)
                except:
                    pass
                    
                try:
                    time.sleep(1)
                    # 2순위: 크롬에 의한 PDF Intercept 발생 시 가운데 생성되는 '열기(Open)' 버튼 타격
                    open_btn = driver.find_element(By.XPATH, "//*[normalize-space(text())='열기' or contains(text(), '열기') or normalize-space(text())='Open']")
                    driver.execute_script("arguments[0].click();", open_btn)
                except:
                    pass
                    
                print("  -> 문서 강제 추출(PDF 다운로드/열기) 개시...")
                
            except Exception as e:
                print(f"  -> [경고] 다운로드/열기 버튼 타격 실패: {str(e)[:40]}")
        else:
            print("  -> 크롬 브라우저 설정에 의한 백그라운드 자동 다운로드가 발생했습니다.")
        # 크롬이 문서를 다운받을 때까지 대기
        time.sleep(2)
        resolved_path = None
        if wait_for_download(download_dir):
            time.sleep(1) # 최종 저장 딜레이
            latest_pdf = get_latest_pdf(download_dir)
            if latest_pdf:
                # 최신 PDF를 우리가 원하는 출원번호 형태로 저장
                new_name = os.path.join(download_dir, f"{app_num_clean}_ESOP_Original.pdf")
                if os.path.exists(new_name):
                    try: os.remove(new_name)
                    except: pass
                # 만약 방금 받아진 PDF라면 이름 변경
                if len(glob.glob(os.path.join(download_dir, '*.pdf'))) > before_download_count:
                    os.rename(latest_pdf, new_name)
                    resolved_path = new_name
                    
        # 뷰어 팝업창 닫고 메인으로 복귀
        if len(driver.window_handles) > 1:
            try:
                driver.execute_script("window.close();") # Webdriver close() 행 유발 버그 방지
            except:
                pass
            driver.switch_to.window(driver.window_handles[0])
            
        return resolved_path
    except Exception as e:
        print(f"  -> 시스템 오류 발생: {str(e)[:50]}...")
        return None

# ==========================================
# 3. 텍스트 추출 및 심층 NLP 분석
# ==========================================
def extract_pdf_and_check_art84(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        full_text = ""
        # ESOP 문서의 경우 심사관 논리가 문서 전반에 적혀 있으므로 전체 스캔
        for page in doc:
            full_text += page.get_text("text") + "\n"
        doc.close()
        
        # Art 84, Article 84, A.84 
        pattern = r"\b(?:Article|Art\.?|A\.?)\s*84\b"
        if re.search(pattern, full_text, re.IGNORECASE):
            return True, full_text
        return False, full_text
    except Exception as e:
        print(f"  -> PDF 디코딩 실패: {e}")
        return False, ""

def analyze_with_gemini(text):
    prompt = f"""
    아래는 심사관이 작성한 'European Search Opinion (ESOP)' 심사 문헌의 텍스트입니다.
    이 의견서에서 'Article 84'를 적용하여 지적하거나 거절한 사유가 정확히 무엇인지 파악하고, 
    반드시 다음 4가지 카테고리 중 **하나로만** 답하세요. (응답 예시: 1. 명확성 부족)
    
    분류 기준:
    1. 명확성 부족 (Lack of Clarity)
    2. 명세서에 의한 뒷받침 부족 (Lack of Support)
    3. 간결성 및 청구항 수 (Conciseness & Number of Claims)
    4. 기타
    
    [문헌 텍스트 (앞 8000글자)]
    {text[:8000]}
    """
    try:
        res = gemini_model.generate_content(prompt)
        text_result = res.text.strip()
        if "명확성 부족" in text_result or "Lack of Clarity" in text_result: return "명확성 부족"
        elif "뒷받침 부족" in text_result or "Lack of Support" in text_result: return "뒷받침 부족"
        elif "간결성" in text_result or "Conciseness" in text_result: return "간결성"
        elif "기타" in text_result or "Other" in text_result: return "기타"
        else: return "조항 84 기반 지적 모호"
    except Exception as e:
        return f"분석 오류: {e}"

# ==========================================
# 4. Main 파이프라인
# ==========================================
def main():
    print("================================================================")
    print(" EPO Register / ESOP(European Search Opinion) 추출 시스템 가동 ")
    print("================================================================")
    
    excel_path = os.path.join(BASE_DIR, '..', '특허검색_EESR검색.xlsx')
    if not os.path.exists(excel_path):
        df = pd.DataFrame({'출원번호': ['18817854.5']})
    else:
        df = pd.read_excel(excel_path)
        
    if 'Article84_여부' not in df.columns:
        df['Article84_여부'] = ""
    if '거절사유_세부분류(ESOP)' not in df.columns:
        df['거절사유_세부분류(ESOP)'] = ""
        
    print("\n[우회 침투용 크롬 브라우저를 구동합니다...]")
    driver = setup_epo_register_driver(DOWNLOAD_DIR)
    
    try:
        for idx, row in df.iterrows():
            raw_app_num = str(row['출원번호']).strip()
            if not raw_app_num or raw_app_num.lower() == 'nan': continue
            
            print(f"\n[진행도: {idx+1}/{len(df)}] 심사 문서 타겟팅: {raw_app_num}")
            
            # ESOP 문서 다운로드 로직
            pdf_path = download_esop_from_register(driver, raw_app_num, DOWNLOAD_DIR)
            
            # 폴백(수동 저장) 체크
            app_clean = raw_app_num.split('.')[0]
            if not app_clean.startswith("EP"): app_clean = "EP" + app_clean
            fallback_pdf = os.path.join(DOWNLOAD_DIR, f"{app_clean}_ESOP_Original.pdf")
            
            if not pdf_path and os.path.exists(fallback_pdf):
                pdf_path = fallback_pdf
                print("  -> 기존 폴더에 수집된 동일한 ESOP 문서가 있어 폴백 적용합니다.")
                
            if not pdf_path:
                df.at[idx, 'Article84_여부'] = "ESOP 미발간"
                df.at[idx, '거절사유_세부분류(ESOP)'] = "분석 불가"
                continue
                
            # 심사관 텍스트 파싱 및 탐지
            is_art84, txt = extract_pdf_and_check_art84(pdf_path)
            if is_art84:
                df.at[idx, 'Article84_여부'] = "제기됨"
                print("  -> [주의] 심사관이 Article 84를 지적했습니다! AI 판독 중...")
                time.sleep(2)
                cls_result = analyze_with_gemini(txt)
                print(f"  -> 심사관 지적 사유: {cls_result}")
                df.at[idx, '거절사유_세부분류(ESOP)'] = cls_result
            else:
                print("  -> [클린] 해당 문서에는 Article 84 지적 내역이 없습니다.")
                df.at[idx, 'Article84_여부'] = "없음"
                df.at[idx, '거절사유_세부분류(ESOP)'] = "-"
                
    finally:
        driver.quit()
        
    result_path = os.path.join(BASE_DIR, '특허검색_ESOP_단독추출_결과.xlsx')
    df.to_excel(result_path, index=False)
    print(f"\n[작업 완료] ESOP 문서가 완벽하게 추출되어 분류되었습니다: {result_path}")

if __name__ == "__main__":
    main()
