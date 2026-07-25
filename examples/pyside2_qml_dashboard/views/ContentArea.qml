import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Pane {
    id: contentArea
    padding: 0

    background: Rectangle {
        color: "transparent"
    }

    // 读取侧栏当前页
    property string activePage: sidebar.currentPage

    StackView {
        id: stack
        anchors.fill: parent
        anchors.margins: 24
        initialItem: homePage
    }

    // ========== Home 页 ==========
    Component {
        id: homePage
        ScrollView {
            clip: true
            ColumnLayout {
                width: stack.width
                spacing: 20

                // 标题区
                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        text: "Home"
                        font.pixelSize: 22
                        font.bold: true
                        color: Theme.isDark ? "#E0E0EF" : "#1A1A1A"
                    }
                    Item { Layout.fillWidth: true }
                    Label {
                        text: "⏵  Running"
                        font.pixelSize: 12
                        color: "#4CAF50"
                    }
                }

                // 发行版卡片网格
                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    rowSpacing: 16
                    columnSpacing: 16

                    DistroCard {
                        distroName: "Debian"
                        version: "12"
                        status: "Running"
                        diskUsage: "12.4 GB"
                        ipAddress: "172.28.45.3"
                    }
                    DistroCard {
                        distroName: "Ubuntu"
                        version: "22.04"
                        status: "Stopped"
                        diskUsage: "8.1 GB"
                        ipAddress: "—"
                    }
                    DistroCard {
                        distroName: "Fedora"
                        version: "39"
                        status: "Stopped"
                        diskUsage: "5.6 GB"
                        ipAddress: "—"
                    }
                    DistroCard {
                        distroName: "Arch"
                        version: "rolling"
                        status: "Running"
                        diskUsage: "3.2 GB"
                        ipAddress: "172.28.45.7"
                    }
                }
            }
        }
    }
}